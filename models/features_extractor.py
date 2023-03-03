#
# Wheatley
# Copyright (c) 2023 Jolibrain
# Authors:
#    Guillaume Infantes <guillaume.infantes@jolibrain.com>
#    Antoine Jacquet <antoine.jacquet@jolibrain.com>
#    Michel Thomazo <thomazo.michel@gmail.com>
#    Emmanuel Benazera <emmanuel.benazera@jolibrain.com>
#
#
# This file is part of Wheatley.
#
# Wheatley is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Wheatley is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Wheatley. If not, see <https://www.gnu.org/licenses/>.
#

from stable_baselines3.common.torch_layers import BaseFeaturesExtractor
import torch
import torch.nn as nn
from torch_geometric.nn.conv import GINEConv, GATv2Conv, EGConv, PDNConv

from models.mlp import MLP
from utils.agent_observation import AgentObservation
from torch_geometric.nn.norm import GraphNorm, BatchNorm
import sys


class FeaturesExtractor(BaseFeaturesExtractor):
    def __init__(
        self,
        observation_space,
        input_dim_features_extractor,
        gconv_type,
        graph_pooling,
        freeze_graph,
        graph_has_relu,
        device,
        max_n_nodes,
        max_n_machines,
        n_mlp_layers_features_extractor,
        n_layers_features_extractor,
        hidden_dim_features_extractor,
        activation_features_extractor,
        n_attention_heads,
        reverse_adj,
        residual=True,
        normalize=False,
        conflicts="att",
    ):

        self.conflicts = conflicts
        self.highmem_conflict_clique = False
        self.mediummem_conflict_clique = True
        self.residual = residual
        self.normalize = normalize
        self.max_n_nodes = max_n_nodes
        features_dim = input_dim_features_extractor + hidden_dim_features_extractor * (n_layers_features_extractor + 1)
        features_dim *= 2
        self.max_n_machines = max_n_machines
        super(FeaturesExtractor, self).__init__(
            observation_space=observation_space,
            features_dim=features_dim,
        )
        self.freeze_graph = freeze_graph
        self.device = device
        self.reverse_adj = reverse_adj

        self.activation_fe = activation_features_extractor
        self.hidden_dim_features_extractor = hidden_dim_features_extractor
        self.gconv_type = gconv_type
        self.graph_has_relu = graph_has_relu
        self.graph_pooling = graph_pooling
        self.n_layers_features_extractor = n_layers_features_extractor
        self.features_extractors = nn.ModuleList()

        self.edge_embedder = nn.ModuleList()
        # self_loops 0
        # precedencies 1
        # graph pooling 2
        # optional conflicts_edges   from 3 to self.max_n_machines+3 + reverse in case of node conflict
        if self.conflicts == "clique":
            nmachineid = self.max_n_machines
        elif self.conflicts == "node":
            nmachineid = self.max_n_machines * 2
        if self.conflicts in ["clique", "node"]:
            for layer in range(self.n_layers_features_extractor):
                if self.gconv_type in ["gcn2"]:
                    self.edge_embedder.append(torch.nn.Embedding(5 + nmachineid + 1, 1))
                else:
                    self.edge_embedder.append(torch.nn.Embedding(5 + nmachineid + 1, hidden_dim_features_extractor))
        else:
            for layer in range(self.n_layers_features_extractor):
                if self.gconv_type in ["gcn2"]:
                    self.edge_embedder.append(torch.nn.Embedding(5, 1))
                else:
                    self.edge_embedder.append(torch.nn.Embedding(5, hidden_dim_features_extractor))

        if self.normalize:
            self.norms = nn.ModuleList()
            self.normsbis = nn.ModuleList()

        self.embedder = MLP(
            n_layers=n_mlp_layers_features_extractor,
            input_dim=input_dim_features_extractor,
            hidden_dim=hidden_dim_features_extractor,
            output_dim=hidden_dim_features_extractor,
            batch_norm=False,
            activation=self.activation_fe,
            device=self.device,
        )

        if self.gconv_type == "gatv2":
            self.mlps = nn.ModuleList()

        if self.normalize:
            self.norm0 = GraphNorm(input_dim_features_extractor)
            self.norm1 = GraphNorm(hidden_dim_features_extractor)

        for layer in range(self.n_layers_features_extractor):

            if self.normalize:
                self.norms.append(GraphNorm(hidden_dim_features_extractor))
                self.normsbis.append(GraphNorm(hidden_dim_features_extractor))

            if self.gconv_type == "gin":
                mlp = torch.nn.Sequential()
                for _ in range(n_mlp_layers_features_extractor - 1):
                    mlp.append(torch.nn.Linear(hidden_dim_features_extractor, hidden_dim_features_extractor))
                    mlp.append(activation_features_extractor())
                    if normalize:
                        mlp.append(BatchNorm(hidden_dim_features_extractor))

                mlp.append(torch.nn.Linear(hidden_dim_features_extractor, hidden_dim_features_extractor))
                if normalize:
                    mlp.append(BatchNorm(hidden_dim_features_extractor))

                self.features_extractors.append(
                    GINConv(
                        MLP(
                            n_layers=n_mlp_layers_features_extractor,
                            input_dim=hidden_dim_features_extractor,
                            hidden_dim=hidden_dim_features_extractor,
                            output_dim=hidden_dim_features_extractor,
                            batch_norm=False if self.freeze_graph else True,
                            activation=self.activation_fe,
                            device=self.device,
                        )
                    )
                )

            elif self.gconv_type == "gatv2":
                self.features_extractors.append(
                    GATv2Conv(
                        in_channels=hidden_dim_features_extractor,
                        out_channels=hidden_dim_features_extractor,
                        heads=n_attention_heads,
                        add_self_loops=True,
                        edge_dim=hidden_dim_features_extractor,
                    )
                )
                self.mlps.append(
                    MLP(
                        n_layers=n_mlp_layers_features_extractor,
                        input_dim=hidden_dim_features_extractor * n_attention_heads,
                        hidden_dim=hidden_dim_features_extractor * n_attention_heads,
                        output_dim=hidden_dim_features_extractor,
                        batch_norm=False,
                        activation=self.activation_fe,
                        device=self.device,
                    )
                )

            elif self.gconv_type == "eg":
                self.features_extractors.append(
                    EGConv(
                        in_channels=hidden_dim_features_extractor,
                        out_channels=hidden_dim_features_extractor,
                        aggregators=["sum", "mean", "symnorm", "min", "max", "var", "std"],
                    )
                )

            elif self.gconv_type == "pdn":
                self.features_extractors.append(
                    PDNConv(
                        in_channels=hidden_dim_features_extractor,
                        out_channels=hidden_dim_features_extractor,
                        edge_dim=1,
                        hidden_channels=16,
                    )
                )

            else:
                print("Unknown gconv type ", self.gconv_type)
                sys.exit()

            self.features_extractors[-1].to(self.device)

        if self.freeze_graph:
            for param in self.parameters():
                param.requires_grad = False

    def forward(self, obs):
        """
        Returns the embedding of the graph concatenated with the embeddings of the nodes
        Note : the output may depend on the number of nodes, but it should not be a
        problem.
        """
        torch.set_printoptions(profile="full")
        observation = AgentObservation.from_gym_observation(obs, False, self.conflicts, self.max_n_machines)
        batch_size = observation.get_batch_size()
        n_nodes = observation.get_n_nodes()

        graph_state = observation.to_graph()
        features, edge_index = graph_state.x, graph_state.edge_index
        n_batch = graph_state.num_graphs

        edge_attr = []
        for layer in range(self.n_layers_features_extractor):
            edge_attr.append(self.edge_embedder[layer](torch.LongTensor([1] * edge_index.shape[1]).to(features.device)))

        if self.normalize:
            batch_id = graph_state.batch

        # offset for adding virtual nodes
        node_offset = graph_state.x.shape[0]

        if self.graph_pooling == "learn":
            graphnode = torch.zeros((n_batch, features.shape[1])).to(features.device)
            ei0 = []
            ei1 = []
            for i in range(n_batch):
                ei0 += [node_offset + i] * (graph_state.ptr[i + 1] - graph_state.ptr[i])
                ei1 += list(range(graph_state.ptr[i], graph_state.ptr[i + 1]))

            if not self.reverse_adj:
                edge_index_0 = torch.cat([edge_index[0], torch.LongTensor(ei0).to(features.device)])
                edge_index_1 = torch.cat([edge_index[1], torch.LongTensor(ei1).to(features.device)])
            else:
                edge_index_0 = torch.cat([edge_index[0], torch.LongTensor(ei1).to(features.device)])
                edge_index_1 = torch.cat([edge_index[1], torch.LongTensor(ei0).to(features.device)])
            edge_index = torch.stack([edge_index_0, edge_index_1])

            features = torch.cat([features, graphnode], dim=0)

            if self.normalize:
                batch_id = torch.cat([batch_id, torch.LongTensor(list(range(n_batch))).to(batch_id.device)])
            for layer in range(self.n_layers_features_extractor):
                edge_attr[layer] = torch.cat(
                    [
                        edge_attr[layer],
                        self.edge_embedder[layer](torch.LongTensor([2] * len(ei0)).to(features.device)),
                    ]
                )
            node_offset += n_batch

        nnodes = graph_state.x.shape[0]
        if self.conflicts == "clique":

            # add bidirectional edges in case of machine conflict in order for GNN to be able
            # to pass messages

            if self.highmem_conflict_clique:

                machine = features[:nnodes, 6 : 6 + self.max_n_machines]
                mmax = torch.max(machine, dim=1)
                machineid = torch.where(mmax[0] == 0, -1, mmax[1])
                aff1 = features[:nnodes, 0].unsqueeze(0).expand(nnodes, nnodes)
                aff2 = features[:nnodes, 0].unsqueeze(1).expand(nnodes, nnodes)
                b1 = graph_state.batch.unsqueeze(0).expand(nnodes, nnodes)
                b2 = graph_state.batch.unsqueeze(1).expand(nnodes, nnodes)
                m1 = machineid.unsqueeze(0).expand(nnodes, nnodes)
                # put m2 unaffected to -2 so that they unaffected task are not considered in conflict
                m2 = torch.where(machineid == -1, -2, machineid).unsqueeze(1).expand(nnodes, nnodes)
                # same machine and same batch
                cond = torch.logical_and(torch.eq(m1, m2), torch.eq(b1, b2))
                # and not both already affected
                cond = torch.logical_and(cond, torch.logical_not(torch.logical_and(aff1, aff2)))
                # and no self loops
                cond = torch.logical_and(
                    cond, torch.logical_not(torch.diag(torch.BoolTensor([True] * nnodes).to(features.device)))
                )
                conflicts = torch.where(cond, 1, 0).nonzero(as_tuple=True)
                ei0new = conflicts[0]
                ei1new = conflicts[1]
                ceanew = machineid[ei0new] + 3

                edge_index_0 = torch.cat([edge_index[0], ei0new])
                edge_index_1 = torch.cat([edge_index[1], ei1new])
                edge_index = torch.stack([edge_index_0, edge_index_1])
                for layer in range(self.n_layers_features_extractor):
                    edge_attr[layer] = torch.cat([edge_attr[layer], self.edge_embedder[layer](ceanew)])
            elif self.mediummem_conflict_clique:

                for bi in torch.arange(0, n_batch, dtype=torch.long, device=features.device):
                    machine = features[graph_state.ptr[bi] : graph_state.ptr[bi + 1], 6 : 6 + self.max_n_machines]
                    nnodes = graph_state.ptr[bi + 1] - graph_state.ptr[bi]
                    aff = features[graph_state.ptr[bi] : graph_state.ptr[bi + 1], 0]
                    mmax = torch.max(machine, dim=1)
                    machineid = torch.where(mmax[0] == 0, -1, mmax[1])
                    aff1 = aff.unsqueeze(0).expand(nnodes, nnodes)
                    aff2 = aff.unsqueeze(1).expand(nnodes, nnodes)
                    m1 = machineid.unsqueeze(0).expand(nnodes, nnodes)
                    m2 = torch.where(machineid == -1, -2, machineid).unsqueeze(1).expand(nnodes, nnodes)
                    cond = torch.logical_and(torch.eq(m1, m2), torch.logical_not(torch.logical_and(aff1, aff2)))
                    cond = torch.logical_and(
                        cond, torch.logical_not(torch.diag(torch.BoolTensor([True] * nnodes).to(features.device)))
                    )
                    conflicts = torch.where(cond, 1, 0).nonzero(as_tuple=True)
                    ei0new = conflicts[0] + graph_state.ptr[bi]
                    ei1new = conflicts[1] + graph_state.ptr[bi]
                    ceanew = machineid[conflicts[0]] + 3
                    edge_index_0 = torch.cat([edge_index[0], ei0new])
                    edge_index_1 = torch.cat([edge_index[1], ei1new])
                    edge_index = torch.stack([edge_index_0, edge_index_1])
                    for layer in range(self.n_layers_features_extractor):
                        edge_attr[layer] = torch.cat([edge_attr[layer], self.edge_embedder[layer](ceanew)])

            else:
                nnodes = graph_state.x.shape[0]
                ei0 = None
                ei1 = None
                cea = None
                featcpu = features[:nnodes].to("cpu")
                machinecpu = featcpu[:, 6 : 6 + self.max_n_machines]
                aff = featcpu[:, 0]
                for bi in torch.arange(0, n_batch, dtype=torch.long):
                    for ni1 in torch.arange(graph_state.ptr[bi], graph_state.ptr[bi + 1], dtype=torch.long):
                        machine1 = machinecpu[ni1]
                        if torch.all(machine1 == 0):
                            continue
                        aff1 = aff[ni1] == 1.0
                        mid = (torch.argmax(machine1) + 3).reshape(1)
                        ni1_ = ni1.reshape(1)
                        for ni2 in torch.arange(ni1 + 1, graph_state.ptr[bi + 1], dtype=torch.long):
                            machine2 = machinecpu[ni2]
                            if torch.all(machine2 == 0):
                                continue
                            aff2 = aff[ni2] == 1.0
                            if torch.equal(machine1, machine2) and (not (aff1 and aff2)):
                                ni2_ = ni2.reshape(1)
                                if ei0 is None:
                                    ei0 = torch.cat([ni1_, ni2_])
                                    ei1 = torch.cat([ni2_, ni1_])
                                    cea = torch.cat([mid, mid])
                                else:
                                    ei0 = torch.cat([ei0, ni1_, ni2_])
                                    ei1 = torch.cat([ei1, ni2_, ni1_])
                                    cea = torch.cat([cea, mid, mid])

                if ei0 is not None:
                    edge_index_0 = torch.cat([edge_index[0], ei0.to(features.device)])
                    edge_index_1 = torch.cat([edge_index[1], ei1.to(features.device)])
                    edge_index = torch.stack([edge_index_0, edge_index_1])
                    cea = cea.to(features.device)
                    for layer in range(self.n_layers_features_extractor):
                        edge_attr[layer] = torch.cat(
                            [
                                edge_attr[layer],
                                self.edge_embedder[layer](cea),
                            ]
                        )

        elif self.conflicts == "node":  # 1 virtual node per machine

            machine = features[:nnodes, 6 : 6 + self.max_n_machines]
            mmax = torch.max(machine, dim=1)
            machineid = torch.where(mmax[0] == 0, -1, mmax[1])
            machine_nodes = torch.zeros((n_batch * self.max_n_machines, features.shape[1])).to(features.device)
            features = torch.cat([features, machine_nodes], dim=0)

            if self.normalize:
                nbid = [[i] * self.max_n_machines for i in range(n_batch)]
                nbid = [x for xs in nbid for x in xs]
                batch_id = torch.cat([batch_id, torch.LongTensor(nbid).to(batch_id.device)])

            idxaffected = torch.where(machineid != -1, 1, 0).nonzero(as_tuple=True)[0]
            machineid = machineid[idxaffected]
            bid = graph_state.batch[idxaffected]
            targetmachinenode = bid * self.max_n_machines + machineid + node_offset
            edge_index_0 = torch.cat([edge_index[0], idxaffected, targetmachinenode])
            edge_index_1 = torch.cat([edge_index[1], targetmachinenode, idxaffected])
            edge_index = torch.stack([edge_index_0, edge_index_1])
            machine_embeder = torch.cat([(machineid + 3), (machineid + 3 + self.max_n_machines)])
            node_offset += n_batch * self.max_n_machines
            for layer in range(self.n_layers_features_extractor):
                edge_attr[layer] = torch.cat([edge_attr[layer], self.edge_embedder[layer](machine_embeder)])

            if self.conflicts != "att":
                features[:, 6 : 6 + self.max_n_machines] = 0

        if not self.reverse_adj:
            edge_index = torch.stack([edge_index[1], edge_index[0]])

        # Compute graph embeddings
        features_list = []

        if self.normalize:
            features = self.norm0(features, batch_id)
        features_list.append(features)
        features = self.embedder(features)
        if self.normalize:
            features = self.norm1(features, batch_id)
        features_list.append(features)

        for layer in range(self.n_layers_features_extractor):
            if self.gconv_type == "gatv2":
                self.features_extractors[layer].fill_value = self.edge_embedder[layer].weight[0]
            features = self.features_extractors[layer](features, edge_index, edge_attr[layer])
            if self.graph_has_relu or self.gconv_type == "gatv2":
                features = torch.nn.functional.elu(features)
            if self.gconv_type == "gatv2":
                features = self.mlps[layer](features)
            if self.normalize:
                features = self.norms[layer](features, batch_id)
            features_list.append(features)
            if self.residual:
                features += features_list[-2]
                if self.normalize:
                    features = self.normsbis[layer](features, batch_id)
        features = torch.cat(features_list, axis=1)  # The final embedding is concatenation of all layers embeddings

        node_features = features[: graph_state.x.shape[0], :]
        node_features = node_features.reshape(batch_size, n_nodes, -1)

        # Create graph embedding and concatenate
        if self.graph_pooling == "max":
            max_elts, max_ind = torch.max(node_features, dim=1)
            graph_embedding = max_elts
        elif self.graph_pooling == "avg":
            graph_pooling = torch.ones(n_nodes, device=self.device) / n_nodes
            graph_embedding = torch.matmul(graph_pooling, node_features)
        elif self.graph_pooling == "learn":
            graph_embedding = features[graph_state.x.shape[0] : graph_state.x.shape[0] + n_batch, :]
        else:
            raise Exception(f"Graph pooling {self.graph_pooling} not recognized. Only accepted pooling are max and avg")

        graph_embedding = graph_embedding.reshape(batch_size, 1, -1)

        # repeat the graph embedding to match the nodes embedding size
        repeated = graph_embedding.expand(node_features.shape)
        return torch.cat((node_features, repeated), dim=2)
