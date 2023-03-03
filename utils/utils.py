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

from copy import deepcopy

import numpy as np
import torch
from typing import List, Optional, Tuple, Union
from collections import defaultdict
import torch_geometric.data

import sys


def get_exp_name(args):

    exp_name = (
        f"{args.n_j}j{args.n_m}m_{args.duration_type}_{args.seed}seed_{args.transition_model_config}_"
        + f"{args.reward_model_config}_{args.fe_type}"
    )
    if args.fe_type != "tokengt":
        exp_name += f"_{args.gconv_type}_{args.graph_pooling}"
    else:
        exp_name += f"_POOL{args.layer_pooling}_DROP{args.dropout}"
    exp_name += f"_L{args.n_layers_features_extractor}_HD{args.hidden_dim_features_extractor}_{args.n_attention_heads}heads_{args.conflicts}_LR{args.lr}"
    if args.dont_normalize_input:
        exp_name += "_DNI"
    if args.fixed_problem:
        exp_name += "_FP"
    if args.load_problem:
        exp_name += "_" + args.load_problem.replace(".txt", "").replace("/", "_")
    if args.freeze_graph:
        exp_name += "_FG"
    if args.insertion_mode == "no_forced_insertion":
        exp_name += "_NFI"
    if args.insertion_mode == "full_forced_insertion":
        exp_name += "_FFI"
    if args.insertion_mode == "choose_forced_insertion":
        exp_name += "_CFI"
    if args.insertion_mode == "slot_locking":
        exp_name += "_SL"
    if args.exp_name_appendix is not None:
        exp_name += "_" + args.exp_name_appendix
    return exp_name


def get_n_features(input_list, max_n_jobs, max_n_machines):
    if "one_hot_machine_id" in input_list:
        input_list.remove("one_hot_machine_id")
    if "selectable" in input_list:
        input_list.remove("selectable")
    # 4 for task completion times, 1 for is_affected, max_n_machines for mandatory one_hot_machine_id
    # 1 for mandatory selectable
    n_features = 6 + max_n_machines
    # most features make 4 values
    n_features += 4 * len(input_list)
    # except one_hot_job_id
    if "one_hot_job_id" in input_list:
        n_features += max_n_jobs - 4
    # except for mopnr
    if "mopnr" in input_list:
        n_features -= 3
    return n_features


def get_path(arg_path, exp_name):
    path = "saved_networks/" + exp_name if arg_path == "saved_networks/default_net" else arg_path
    return path


def generate_deterministic_problem(n_jobs, n_machines, high):
    """
    Generate a random intance of a JSS problem, of size (n_jobs, n_machines), with times comprised between specified
    lower and higher bound.
    Note that durations is of shape[n_jobs, n_machines, 4], but its only 4 repetitions of an array of shape
    [n_jobs, n_machines]
    """
    durations = np.random.randint(low=1, high=high, size=(n_jobs, n_machines, 1))
    durations = np.repeat(durations, repeats=4, axis=2)
    affectations = np.expand_dims(np.arange(0, n_machines), axis=0)
    affectations = affectations.repeat(repeats=n_jobs, axis=0)
    affectations = _permute_rows(affectations)
    return affectations, durations


def generate_problem_distrib(n_jobs, n_machines, duration_mode_bounds, duration_delta):
    """
    Generate a problem distribution, using duration_mode_bounds = [v1, v2] and duration_delta=[d1, d2].

    The durations variable is a numpy.array of shape [n_jobs, n_machines, 4]
    durations[:, :, 0] will store real durations (and is left unaffected).
    (durations[:, :, 1], durations[:, :, 3], durations[:, :, 2]) will be the (left, mode, right) value of a triangular
    distribution (see https://numpy.org/doc/stable/reference/random/generated/numpy.random.triangular.html)

    durations[:, :, 3] is randomly sampled in the [v1, v2] interval
    durations[:, :, 1] is randomly sampled in the [durations[:, :, 3] - d1, durations[:, :, 3] - 1] interval
    durations[:, :, 2] is randomly sampled in the [durations[:, :, 3] + 1, durations[:, :, 3] + d2] interval
    """
    durations = np.empty((n_jobs, n_machines, 4), dtype=np.int32)

    # Sampling the modes, using duration_mode_bounds
    durations[:, :, 3] = np.random.randint(duration_mode_bounds[0], duration_mode_bounds[1], size=(n_jobs, n_machines))
    for j in range(n_jobs):
        for m in range(n_machines):
            # Sampling the left
            dd = np.random.randint(1, duration_delta[0])
            durations[j, m][1] = max(1, durations[j, m][3] - dd)
            # Sampling the right
            dd = np.random.randint(1, duration_delta[1])
            durations[j, m][2] = durations[j, m][3] + dd

    # Each row of the affectations array is a permutation of [0, n_machines[
    affectations = np.expand_dims(np.arange(0, n_machines), axis=0)
    affectations = affectations.repeat(repeats=n_jobs, axis=0)
    affectations = _permute_rows(affectations)
    return affectations, durations


def generate_problem_durations(durations):
    """
    Generate the problem real durations, using a triangular distribution.
    durations[:, :, 0] with a triangular distribution using (durations[:, :, 1], durations[:, :, 3], durations[:, :, 2])
    as (left, mode, right) tuple (see https://numpy.org/doc/stable/reference/random/generated/numpy.random.triangular.html)
    """
    ret_durations = deepcopy(durations)
    n_jobs = durations.shape[0]
    n_machines = durations.shape[1]
    for j in range(n_jobs):
        for m in range(n_machines):
            if durations[j, m, 3] < 0.0:
                continue
            if durations[j, m, 1] == durations[j, m, 2]:
                ret_durations[j, m, 0] = durations[j, m, 1]
            else:
                if durations[j, m][1] > durations[j, m][3]:
                    print(durations[j, m][1], durations[j, m][3])
                ret_durations[j, m, 0] = np.random.triangular(durations[j, m][1], durations[j, m][3], durations[j, m][2])
    return ret_durations


def _permute_rows(x):
    """
    x is a bidimensional numpy array
    """
    ix_i = np.tile(np.arange(x.shape[0]), (x.shape[1], 1)).transpose()
    ix_j = np.random.sample(x.shape).argsort(axis=1)
    return x[ix_i, ix_j]


def generate_data(n_j, n_m, max_duration, seed=200, n_problems=100):
    np.random.seed(seed)
    data = np.array([generate_deterministic_problem(n_j, n_m, max_duration) for _ in range(n_problems)])
    return data


def node_to_job_and_task(node_id, n_machines):
    return node_id // n_machines, node_id % n_machines


def job_and_task_to_node(job_id, task_id, n_machines):
    return job_id * n_machines + task_id


def load_taillard_problem(problem_file, taillard_offset=True, deterministic=True):
    # http://jobshop.jjvh.nl/explanation.php#taillard_def

    if not deterministic:
        print("Loading problem with uncertainties, using extended taillard format")

    with open(problem_file, "r") as f:
        line = next(f)
        while line[0] == "#":
            line = next(f)

        # header
        header = line
        head_list = [int(i) for i in header.split()]
        assert len(head_list) == 2
        n_j = head_list[0]
        n_m = head_list[1]

        line = next(f)
        while line[0] == "#":
            line = next(f)

        # matrix of durations
        np_lines = []
        for j in range(n_j):
            dur_list = [float(i) for i in line.split()]
            np_lines.append(np.array(dur_list))
            line = next(f)
        durations = np.stack(np_lines)

        if deterministic:
            durations = np.expand_dims(durations, axis=2)
            durations = np.repeat(durations, 4, axis=2)
        else:
            mode_durations = durations

            while line[0] == "#":
                line = next(f)

            np_lines = []
            for j in range(n_j):
                dur_list = [float(i) for i in line.split()]
                np_lines.append(np.array(dur_list))
                line = next(f)
            min_durations = np.stack(np_lines)

            while line[0] == "#":
                line = next(f)

            np_lines = []
            for j in range(n_j):
                dur_list = [float(i) for i in line.split()]
                np_lines.append(np.array(dur_list))
                line = next(f)
            max_durations = np.stack(np_lines)

            real_durations = np.zeros((n_j, n_m)) - 1

            durations = np.stack([real_durations, min_durations, max_durations, mode_durations], axis=2)

        while line[0] == "#":
            line = next(f)

        # matrix of affectations
        if taillard_offset:
            toffset = 1
        else:
            toffset = 0
        np_lines = []
        for j in range(n_j):
            aff_list = [int(i) - toffset for i in line.split()]  # Taillard spec has machines id start at 1
            np_lines.append(np.array(aff_list))
            line = next(f, "")
            if line == "":
                break
        affectations = np.stack(np_lines)

        return n_j, n_m, affectations, durations


def check_sanity(affectations, durations):
    for job, (affs, durs) in enumerate(zip(affectations, durations)):
        for machine, (aff, dur) in enumerate(zip(affs, durs)):
            if aff == -1 and any(x != -1 for x in dur):
                raise Exception(
                    "affectations and durations should be only -1 for job " + str(job) + " machine " + str(machine)
                )
            if aff != -1 and any(x == -1 for x in dur[1:]):
                raise Exception(
                    "affectations and durations should not be -1 for job " + str(job) + " machine " + str(machine)
                )


def load_problem(
    problem_file, taillard_offset=False, deterministic=True, load_from_job=0, load_max_jobs=-1, generate_bounds=-1.0
):
    # Customized problem loader
    # - support for bounded duration uncertainty
    # - support for unattributed machines
    # - support for columns < number of machines

    print("generate_bounds=", generate_bounds)

    if not deterministic:
        print("Loading problem with uncertainties, using customized format")
        if generate_bounds > 0:
            print("Generating random duration bounds of ", generate_bounds, " %")

    with open(problem_file, "r") as f:
        line = next(f)
        while line[0] == "#":
            line = next(f)

        # header
        header = line
        head_list = [int(i) for i in header.split()]
        assert len(head_list) == 2
        n_j = head_list[0]
        n_m = head_list[1]

        line = next(f)
        while line[0] == "#":
            line = next(f)

        # matrix of durations
        np_lines = []
        for j in range(n_j):
            dur_list = []
            for i in line.split():
                add_dur = float(i)
                if add_dur == 0:
                    add_dur = 0.1
                elif add_dur < 0:
                    add_dur = -1.0
                dur_list.append(add_dur)
            while len(dur_list) < n_m:
                dur_list.append(-1.0)
            np_lines.append(np.array(dur_list))
            line = next(f)
        durations = np.stack(np_lines)

        if deterministic:
            durations = np.expand_dims(durations, axis=2)
            durations = np.repeat(durations, 4, axis=2)
        elif generate_bounds > 0.0:
            mode_durations = durations
            min_durations = np.subtract(
                durations,
                generate_bounds * durations,
                out=durations.copy(),
                where=durations != -1,
            )
            max_durations = np.add(durations, generate_bounds * durations, out=durations.copy(), where=durations != -1)
            real_durations = np.zeros((n_j, n_m)) - 1
            durations = np.stack([real_durations, min_durations, max_durations, mode_durations], axis=2)
            # sys.exit()
        else:
            mode_durations = durations

            while line[0] == "#":
                line = next(f)

            np_lines = []
            for j in range(n_j):
                dur_list = []
                for i in line.split():
                    add_dur = float(i)
                    if add_dur == 0:
                        add_dur = 0.1
                    elif add_dur < 0:
                        add_dur = -1.0
                    dur_list.append(add_dur)
                while len(dur_list) < n_m:
                    dur_list.append(-1.0)
                np_lines.append(np.array(dur_list))
                line = next(f)
            min_durations = np.stack(np_lines)

            while line[0] == "#":
                line = next(f)

            np_lines = []
            for j in range(n_j):
                dur_list = []
                for i in line.split():
                    add_dur = float(i)
                    if add_dur == 0:
                        add_dur = 0.1
                    elif add_dur < 0:
                        add_dur = -1.0
                    dur_list.append(add_dur)
                while len(dur_list) < n_m:
                    dur_list.append(-1.0)
                np_lines.append(np.array(dur_list))
                line = next(f)
            max_durations = np.stack(np_lines)

            real_durations = np.zeros((n_j, n_m)) - 1

            durations = np.stack([real_durations, min_durations, max_durations, mode_durations], axis=2)

        while line[0] == "#":
            line = next(f)

        # matrix of affectations
        if taillard_offset:
            toffset = 1
        else:
            toffset = 0
        np_lines = []
        for j in range(n_j):
            aff_list = [int(i) - toffset for i in line.split()]  # Taillard spec has machines id start at 1
            while len(aff_list) < n_m:
                aff_list.append(-1)
            np_lines.append(np.array(aff_list))
            line = next(f, "")
            if line == "":
                break
        affectations = np.stack(np_lines)

        if load_max_jobs == -1:
            load_max_jobs = n_j

        affectations = affectations[load_from_job : load_from_job + load_max_jobs]
        durations = durations[load_from_job : load_from_job + load_max_jobs]

        check_sanity(affectations, durations)

        return len(affectations), n_m, affectations, durations


def put_back_one_hot_encoding_unbatched(
    features,
    max_n_machines,
):
    machineid = features[:, :, 6].long()
    idxaffected = torch.where(machineid != -1, 1, 0).nonzero(as_tuple=True)
    features[idxaffected][:, 6 : 6 + max_n_machines] = torch.nn.functional.one_hot(
        features[idxaffected][:, 6].long(), num_classes=max_n_machines
    ).float()

    idxnonaffected = torch.where(machineid == -1, 1, 0).nonzero(as_tuple=True)
    features[idxnonaffected][:, 6 : 6 + max_n_machines] = torch.zeros(len(idxnonaffected[0]), max_n_machines) - 1
    return features


def put_back_one_hot_encoding_batched(
    features,
    num_nodes,
    max_n_machines,
):
    machineid = features[:num_nodes, 6].long()
    idxaffected = torch.where(machineid != -1, 1, 0).nonzero(as_tuple=True)[0]
    features[idxaffected, 6 : 6 + max_n_machines] = torch.nn.functional.one_hot(
        features[idxaffected, 6].long(), num_classes=max_n_machines
    ).float()

    idxnonaffected = torch.where(machineid == -1, 1, 0).nonzero(as_tuple=True)[0]
    features[idxnonaffected, 6 : 6 + max_n_machines] = (
        torch.zeros(len(idxnonaffected), max_n_machines, device=features.device) - 1
    )
    return features


def lr_schedule_linear(top, end, percent_warmup, x_orig):
    x = 1 - x_orig
    if x < percent_warmup:
        lr = end + (top - end) * (x / percent_warmup)
    else:
        lr = top - (top - end) * ((x - percent_warmup) / (1 - percent_warmup))

    return lr
