# Copyright (c) OpenMMLab. All rights reserved.
from xml.dom import HierarchyRequestErr
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from mmcv.cnn import ConvModule, constant_init, normal_init, xavier_init

from .gread import global_add_pool, global_mean_pool, global_max_pool
from .gread import GlobalAttention, Set2Set
import scipy.spatial as sp
from torch_scatter import scatter, scatter_add, scatter_max

from ..builder import NECKS, build_loss


@NECKS.register_module()
class PretrainNeck(nn.Module):
    """ A simple classification head.

    Args:
        num_classes (int): Number of classes to be classified.
        in_channels (int): Number of channels in input feature.
        loss_cls (dict): Config for building loss. Default: dict(type='CrossEntropyLoss')
        dropout (float): Probability of dropout layer. Default: 0.5.
        init_std (float): Std value for Initiation. Default: 0.01.
        kwargs (dict, optional): Any keyword argument to be used to initialize
            the head.
    """

    def __init__(self,
                 in_channels,
                 read_op,
                 num_position,
                 num_hierarchy=3,
                 declay = 0.4,
                 gamma = 0.1,
                 dropout=0.5,
                 init_std=0.01,
                 **kwargs):
        super().__init__()

        self.dropout_ratio = dropout
        self.init_std = init_std
        if self.dropout_ratio != 0:
            self.dropout = nn.Dropout(p=self.dropout_ratio)
        else:
            self.dropout = None
        self.emb_dim = in_channels
        self.read_op = read_op
        self.num_position = num_position
        self.gamma = gamma
        self.num_hierarchy = num_hierarchy 
        self.declay = declay
        self.protos = []
        self.node_type = 5
        # self.protos = [torch.nn.Parameter(torch.zeros(int(num_position*(declay**i)), in_channels), requires_grad=True) for i in range(num_hierarchy)]
        for i in range(num_hierarchy):
            self.protos.append(torch.nn.Parameter(torch.zeros(int(num_position*(declay**i)), in_channels), requires_grad=True))
            torch.nn.init.xavier_normal_(self.protos[i])
        #     # t
        # torch.nn.init.xavier_normal_(self.protos)
        # normal_init(self.protos, std=self.init_std)
    
        if read_op == 'sum':
            self.gread = global_add_pool
        elif read_op == 'mean':
            self.gread = global_mean_pool 
        elif read_op == 'max':
            self.gread = global_max_pool 
        elif read_op == 'attention':
            self.gread = GlobalAttention(gate_nn = torch.nn.Linear(in_channels, 1))
        elif read_op == 'set2set':
            self.gread = Set2Set(in_channels, processing_steps = 2) 
        else:
            raise ValueError("Invalid graph readout type.")

        # self.in_c = in_channels
        # self.rep_dim = in_channels*num_position
        self.fc_cls = nn.Linear(in_channels, self.node_type)

    def init_weights(self):
        """Initiate the parameters from scratch."""
        normal_init(self.protos, std=self.init_std)

    def forward(self, x):
        """Defines the computation performed at every call.

        Args:
            x (torch.Tensor): The input data.

        Returns:
            torch.Tensor: The classification scores for input samples.
        """
        if isinstance(x, list):
            for item in x:
                assert len(item.shape) == 2
            x = [item.mean(dim=0) for item in x]
            x = torch.stack(x)

        assert len(x.shape) == 5
        N, M, C, T, V = x.shape
        # AlineLoss = self.get_aligncost(x)
        x = x.mean(1)

        x = x.permute(0, 2, 3, 1).reshape(-1, C)
        batch = torch.zeros(x.shape[0]).type(torch.int64).to(x.device)
        for i in range(N):
            batch[i*V*T:(i+1)*V*T] =i
        for i in range(self.num_hierarchy):   
            size = N
            A = self.get_alignment(x, i)
            sbatch = int(self.num_position*(self.declay**i)) * batch + torch.max(A, dim=1)[1]
            ssize = int(self.num_position*(self.declay**i)) * (batch.max().item() + 1)
            x = self.gread(x, sbatch, size=ssize)
            # x = x.reshape(N, int(self.num_position*(0.4**i)), -1)
            batch = torch.zeros(x.shape[0]).type(torch.int64).to(x.device)
            for num in range(N):
                batch[num*int(self.num_position*(0.4**i)):(num+1)*int(self.num_position*(0.4**i))] =num
            # x = x.mean(dim=1)
        x = x.reshape(N, int(self.num_position*(0.4**(self.num_hierarchy-1))), -1)
        x = x.mean(1)
        return x
        
    def init_protos(self, protos):
        self.protos.data = protos

    def get_intracost(self, x, x_modify, tau = 0.1):   
        N,M,C,T,V = x_modify.shape
        x_modify = x_modify.reshape(N*M, C, T*V).permute(0,2,1)
        x = x.reshape(N*M, C, T*V).permute(0,2,1)
        sim = torch.einsum('bnc,bmt->bnm', (x, x_modify))
        sim = F.normalize(sim, p=2, dim=1)
        sim = torch.exp(sim / tau)

        mask = torch.eye(sim.shape[1]).to(x.device).unsqueeze(0).repeat(sim.shape[0],1,1)
        positive_sim = sim *mask
        positive_ratio = positive_sim.sum(1) / (sim.sum(1) + 1e-6)
        NCE_loss = -torch.log(positive_ratio).mean()

        return NCE_loss

    def get_intercost(self, x, x_modify, tau=0.1):   
        x = self.graph_gread(x)
        x_modify = self.graph_gread(x_modify)    
        sim = torch.einsum('bc,dc->bd', (x, x_modify))
        sim = F.normalize(sim, p=2, dim=1)
        sim = torch.exp(sim / tau)
        mask = torch.eye(sim.shape[0]).to(x.device)
        positive = (sim * mask).sum(0)
        negative = (sim * (1 - mask)).sum(0)
        positive_ratio = positive / (positive + negative + 1e-6)
        NCE_loss = -torch.log(positive_ratio).mean()

        return NCE_loss

    def node_precost(self, x, node_type, mask):
        N,M,C,T,V = x.shape
        x = x.mean(3).permute(0,1,3,2).reshape(-1,C)
        node_pre = self.fc_cls(x)
        node_type = node_type.unsqueeze(0).unsqueeze(2).repeat(N*M,1,1)
        node_label = torch.zeros(N*M, V, 5).scatter_(2,node_type,1)
        # node_label = node_label.unsqueeze(1).repeat(1,T,1,1).reshape(-1,5).to(x.device)
        node_label = node_label.reshape(-1,5).to(x.device)
        # node_type = node_type.unsqueeze(0).unsqueeze(0).unsqueeze(0).unsqueeze(0).repeat(N,M,1,T,1)
        # node_type = node_type.permute(0,1,3,4,2).reshape(-1,1).to(x.device)
        loss = nn.CrossEntropyLoss(reduce=False)
        node_loss = loss(node_pre, node_label)

        mask = mask[:,:,0,:,:].reshape(-1,1).squeeze(-1)

        return torch.sum(node_loss*mask)/torch.sum(mask)



    def graph_gread(self, x):
        pool = nn.AdaptiveAvgPool2d(1)
        N, M, C, T, V = x.shape
        x = x.reshape(N * M, C, T, V)
        x = pool(x)
        x = x.reshape(N, M, C)
        x = x.mean(dim=1)
        return x
    def get_alignment(self, x, i):
        # D = self._compute_distance_matrix(x, self.protos)
        D = 1 - torch.cosine_similarity(x.unsqueeze(1), self.protos[i].unsqueeze(0).to(x.device), dim=2)
        A = torch.zeros_like(D).scatter_(1, torch.argmin(D, dim=1, keepdim=True), 1.)
        return A 

    def get_aligncost(self, x):
        N, M, C, T, V = x.shape
        # AlineLoss = self.get_aligncost(x)
        x = x.mean(1)

        x = x.permute(0, 2, 3, 1).reshape(-1, C)
        batch = torch.zeros(x.shape[0]).type(torch.int64).to(x.device)
        Hieraechy_loss = []
        alin_loss = 0
        for i in range(N):
            batch[i*V*T:(i+1)*V*T] =i
        for i in range(self.num_hierarchy):
        # D = self._compute_distance_matrix(x, self.protos)
            # D = 1 - torch.cosine_similarity(x.unsqueeze(1), self.protos[i].unsqueeze(0).to(x.device), dim=2)
            # A = torch.zeros_like(D).scatter_(1, torch.argmin(D, dim=1, keepdim=True), 1.)
            # # D = 1 - self.cos(x, self.protos)
            # if self.gamma == 0:
            #     D = torch.min(D, dim=1)[0]
            # else:
            #     D = -self.gamma * torch.log(torch.sum(torch.exp(-D/self.gamma), dim=1) + 1e-12)    
            # N1 = scatter_add(A, batch, dim=0)
            # # D = D.unsqueeze(1)*A
            # D_loss = scatter_add(D, batch, dim=0)
            # D_loss = torch.mean(D_loss/(N1 + 1e-12))
            # Hieraechy_loss.append(D_loss)
            # alin_loss += D_loss

            D = 1 - torch.cosine_similarity(x.unsqueeze(1), self.protos[i].unsqueeze(0).to(x.device), dim=2)
            A = torch.zeros_like(D).scatter_(1, torch.argmin(D, dim=1, keepdim=True), 1.)
            N1 = scatter_add(A, batch, dim=0)
            if self.gamma == 0:
                D = torch.min(D, dim=1)[0]
            else:
                D = -self.gamma * torch.log(torch.sum(torch.exp(-D/self.gamma), dim=1) + 1e-12)
            index = scatter_add(D, batch, dim=0)
            # N1 = torch.log(torch.sum((1.0/(N1+1e-4)),dim=1))
            D_loss = torch.mean(index)


            # N1 = torch.zeros(D.shape[0], batch.max().item() + 1, device=D.device).scatter_(1, batch.unsqueeze(dim=1), 1.)
            # N1/= N1.sum(dim=0, keepdim=True)
            # D_loss = torch.mean(D / N1.sum(dim=1), dim=0)
            Hieraechy_loss.append(D_loss)
            alin_loss += D_loss




            A1 = self.get_alignment(x, i)
            sbatch = int(self.num_position*(self.declay**i)) * batch + torch.max(A1, dim=1)[1]
            ssize = int(self.num_position*(self.declay**i)) * (batch.max().item() + 1)
            x = self.gread(x, sbatch, size=ssize)
            # x = x.reshape(N, int(self.num_position*(0.4**i)), -1)
            batch = torch.zeros(x.shape[0]).type(torch.int64).to(x.device)
            for num in range(N):
                batch[num*int(self.num_position*(0.4**i)):(num+1)*int(self.num_position*(0.4**i))] =num

        return Hieraechy_loss, alin_loss
        # N = torch.zeros(D.shape[0], batch.max().item() + 1, device=D.device).scatter(1,)
        # N = torch.zeros(D.shape[0], batch.max().item() + 1, device=D.device).scatter_(1, batch.unsqueeze(dim=1), 1.)
        # N /= N.sum(dim=0, keepdim=True)
        # return torch.mean(D / N.sum(dim=1), dim=0)
        

    def _compute_distance_matrix(self, h, p):
        h_ = torch.pow(torch.pow(h, 2).sum(1, keepdim=True), 0.5)
        p_ = torch.pow(torch.pow(p, 2).sum(1, keepdim=True), 0.5)
        hp_ = torch.matmul(h_, p_.transpose(0, 1))
        hp = torch.matmul(h, p.transpose(0, 1))
        return 1 - hp / (hp_ + 1e-12)
