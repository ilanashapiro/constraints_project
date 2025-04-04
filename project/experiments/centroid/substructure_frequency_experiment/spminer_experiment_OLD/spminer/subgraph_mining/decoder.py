import argparse
import csv
from itertools import combinations
import time
import os, sys, json, re

# from deepsnap.batch import Batch
import numpy as np
import torch
import torch.optim as optim
import torch.nn as nn
import torch.nn.functional as F
from tqdm import tqdm

from torch_geometric.datasets import TUDataset, PPI
from torch_geometric.datasets import Planetoid, KarateClub, QM7b
from torch_geometric.data import DataLoader
import torch_geometric.utils as pyg_utils

import torch_geometric.nn as pyg_nn
from matplotlib import cm
sys.path.append("/home/ubuntu/project/experiments/centroid/substructure_frequency_experiment/spminer")
from common import data
from common import models
from common import utils
from common import combined_syn
from subgraph_mining.config import parse_decoder
from subgraph_matching.config import parse_encoder
from subgraph_mining.search_agents import GreedySearchAgent, MCTSSearchAgent

import matplotlib.pyplot as plt

import random
from scipy.io import mmread
import scipy.stats as stats
from sklearn.manifold import TSNE
from sklearn.cluster import KMeans, AgglomerativeClustering
from collections import defaultdict
from itertools import permutations
from queue import PriorityQueue
import matplotlib.colors as mcolors
import networkx as nx
import pickle
import torch.multiprocessing as mp
from sklearn.decomposition import PCA

DIRECTORY = "/home/ubuntu/project"
# DIRECTORY = "/Users/ilanashapiro/Documents/constraints_project/project"
# DIRECTORY = "/home/ilshapiro/project"

def make_plant_dataset(size):
    generator = combined_syn.get_generator([size])
    random.seed(3001)
    np.random.seed(14853)
    # PATTERN 1
    pattern = generator.generate(size=10)
    # PATTERN 2
    #pattern = nx.star_graph(9)
    # PATTERN 3
    #pattern = nx.complete_graph(10)
    # PATTERN 4
    #pattern = nx.Graph()
    #pattern.add_edges_from([(1, 2), (2, 3), (3, 4), (4, 5), (5, 6),
    #    (6, 7), (7, 2), (7, 8), (8, 9), (9, 10), (10, 6)])
    nx.draw(pattern, with_labels=True)
    plt.savefig("plots/cluster/plant-pattern.png")
    plt.close()
    graphs = []
    for i in range(1000):
        graph = generator.generate()
        n_old = len(graph)
        graph = nx.disjoint_union(graph, pattern)
        for j in range(1, 3):
            u = random.randint(0, n_old - 1)
            v = random.randint(n_old, len(graph) - 1)
            graph.add_edge(u, v)
        graphs.append(graph)
    return graphs

def pattern_growth(dataset, task, args):
    # init model
    if args.method_type == "end2end":
        model = models.End2EndOrder(1, args.hidden_dim, args)
    elif args.method_type == "mlp":
        model = models.BaselineMLP(1, args.hidden_dim, args)
    else:
        model = models.OrderEmbedder(1, args.hidden_dim, args)
    model.to(utils.get_device(args.gpu_id))
    model.eval()
    model.load_state_dict(torch.load(args.model_path,
        map_location=utils.get_device(args.gpu_id)))
    
    if task == "graph-labeled":
        dataset, labels = dataset

    # load data
    if task == "stg":
      corpora_path = f"{DIRECTORY}/experiments/centroid/substructure_frequency_experiment/corpora/composer_centroid_input_graphs.txt"

      with open(corpora_path, 'r') as file:
        composer_corpus_pieces_paths = json.load(file)
      def load_graph(file_path):
        with open(file_path, 'rb') as f:
          return pickle.load(f).to_undirected()
      graphs = []
      for composer, filepaths in composer_corpus_pieces_paths.items():
        if composer == args.composer:
          graphs = [load_graph(re.sub(r'^.*?/project', DIRECTORY, file_path)) for file_path in filepaths]

          for graph in graphs:
            for node in graph.nodes:
              if node.startswith("Pr"):
                graph.nodes[node]['label'] = hash(node)
              else:
                graph.nodes[node]['label'] = hash(node[0])
            
            for u, v, data in graph.edges(data=True):
              source_label = graph.nodes[u]['label']
              sink_label = graph.nodes[v]['label']
              data['label'] = hash(f"{source_label}^{sink_label}")
    else:
      print(len(dataset), "graphs")
      if task == "graph-labeled": print("using label 0")
      graphs = []
      for i, graph in enumerate(dataset):
          if task == "graph-labeled" and labels[i] != 0: continue
          if task == "graph-truncate" and i >= 1000: break
          if not type(graph) == nx.Graph:
              graph = pyg_utils.to_networkx(graph).to_undirected()
          graphs.append(graph)
    print("search strategy:", args.search_strategy)
    neighs_pyg, neighs = [], []
    
    if args.use_whole_graphs:
        neighs = graphs
    else:
        anchors = []
        if args.sample_method == "radial":
            for i, graph in enumerate(graphs):
                print(i)
                for j, node in enumerate(graph.nodes):
                    # if len(dataset) <= 10 and j % 100 == 0: print(i, j)
                    if len(graphs) <= 10 and j % 100 == 0: print(i, j)
                    if args.use_whole_graphs:
                        neigh = graph.nodes
                    else:
                        neigh = list(nx.single_source_shortest_path_length(graph,
                            node, cutoff=args.radius).keys())
                        if args.subgraph_sample_size != 0:
                            neigh = random.sample(neigh, min(len(neigh),
                                args.subgraph_sample_size))
                    if len(neigh) > 1:
                        neigh = graph.subgraph(neigh)
                        if args.subgraph_sample_size != 0:
                            neigh = neigh.subgraph(max(
                                nx.connected_components(neigh), key=len))
                        neigh = nx.convert_node_labels_to_integers(neigh)
                        neigh.add_edge(0, 0)
                        neighs.append(neigh)
        elif args.sample_method == "tree":
            start_time = time.time()
            for j in tqdm(range(args.n_neighborhoods)):
                graph, neigh = utils.sample_neigh(graphs,
                    random.randint(args.min_neighborhood_size,
                        args.max_neighborhood_size))
                neigh = graph.subgraph(neigh)
                neigh = nx.convert_node_labels_to_integers(neigh)
                neigh.add_edge(0, 0)
                neighs.append(neigh)
                if args.node_anchored:
                    anchors.append(0)   # after converting labels, 0 will be anchor

    embs = []
    if len(neighs) % args.batch_size != 0:
        print("WARNING: number of graphs not multiple of batch size")
    for i in range(len(neighs) // args.batch_size):
        #top = min(len(neighs), (i+1)*args.batch_size)
        top = (i+1)*args.batch_size
        with torch.no_grad():
            batch = utils.batch_nx_graphs(neighs[i*args.batch_size:top],
                anchors=anchors if args.node_anchored else None)
            emb = model.emb_model(batch)
            emb = emb.to(torch.device("cpu"))

        embs.append(emb)

    if args.analyze:
        embs_np = torch.stack(embs).numpy()
        plt.scatter(embs_np[:,0], embs_np[:,1], label="node neighborhood")

    if args.search_strategy == "mcts":
        assert args.method_type == "order"
        agent = MCTSSearchAgent(args.min_pattern_size, args.max_pattern_size,
            model, graphs, embs, node_anchored=args.node_anchored,
            analyze=args.analyze, out_batch_size=args.out_batch_size, gpu_id=args.gpu_id)
    elif args.search_strategy == "greedy":
        agent = GreedySearchAgent(args.min_pattern_size, args.max_pattern_size,
            model, graphs, embs, node_anchored=args.node_anchored,
            analyze=args.analyze, model_type=args.method_type,
            out_batch_size=args.out_batch_size, gpu_id=args.gpu_id)
    out_graphs = agent.run_search(args.n_trials)
    print(time.time() - start_time, "TOTAL TIME")
    x = int(time.time() - start_time)
    print(x // 60, "mins", x % 60, "secs")

    # visualize out patterns
    count_by_size = defaultdict(int)
    for pattern in out_graphs:
        if args.node_anchored:
            colors = ["red"] + ["blue"]*(len(pattern)-1)
            nx.draw(pattern, node_color=colors, with_labels=True)
        else:
            nx.draw(pattern)
        
        os.makedirs(args.plots_dir, exist_ok=True)
        print(f"Saving plots: {args.plots_dir}/{len(pattern)}-{count_by_size[len(pattern)]}.png")
        plt.savefig(f"{args.plots_dir}/{len(pattern)}-{count_by_size[len(pattern)]}.png")
        plt.savefig(f"{args.plots_dir}/{len(pattern)}-{count_by_size[len(pattern)]}.pdf")
        plt.close()
        count_by_size[len(pattern)] += 1

    if not os.path.exists("results"):
        os.makedirs("results")
    with open(args.out_path, "wb") as f:
        pickle.dump(out_graphs, f)

def main(override_args=None):
    if not os.path.exists("plots/cluster"):
        os.makedirs("plots/cluster")

    parser = argparse.ArgumentParser(description='Decoder arguments')
    parse_encoder(parser)
    parse_decoder(parser)
    args = parser.parse_args()

    if override_args:
      for key, value in vars(override_args).items():
        setattr(args, key, value)  # Override with the passed-in values
    
    dataset = None

    print("Using dataset {}".format(args.dataset))
    if args.dataset == 'enzymes':
        dataset = TUDataset(root='/tmp/ENZYMES', name='ENZYMES')
        task = 'graph'
    elif args.dataset == 'cox2':
        dataset = TUDataset(root='/tmp/cox2', name='COX2')
        task = 'graph'
    elif args.dataset == 'reddit-binary':
        dataset = TUDataset(root='/tmp/REDDIT-BINARY', name='REDDIT-BINARY')
        task = 'graph'
    elif args.dataset == 'dblp':
        dataset = TUDataset(root='/tmp/dblp', name='DBLP_v1')
        task = 'graph-truncate'
    elif args.dataset == 'coil':
        dataset = TUDataset(root='/tmp/coil', name='COIL-DEL')
        task = 'graph'
    elif args.dataset == 'stg':
        task = 'stg'
    elif args.dataset.startswith('roadnet-'):
        graph = nx.Graph()
        with open("data/{}.txt".format(args.dataset), "r") as f:
            for row in f:
                if not row.startswith("#"):
                    a, b = row.split("\t")
                    graph.add_edge(int(a), int(b))
        dataset = [graph]
        task = 'graph'
    elif args.dataset == "ppi":
        dataset = PPI(root="/tmp/PPI")
        task = 'graph'
    elif args.dataset in ['diseasome', 'usroads', 'mn-roads', 'infect']:
        fn = {"diseasome": "bio-diseasome.mtx",
            "usroads": "road-usroads.mtx",
            "mn-roads": "mn-roads.mtx",
            "infect": "infect-dublin.edges"}
        graph = nx.Graph()
        with open("data/{}".format(fn[args.dataset]), "r") as f:
            for line in f:
                if not line.strip(): continue
                a, b = line.strip().split(" ")
                graph.add_edge(int(a), int(b))
        dataset = [graph]
        task = 'graph'
    elif args.dataset.startswith('plant-'):
        size = int(args.dataset.split("-")[-1])
        dataset = make_plant_dataset(size)
        task = 'graph'

    pattern_growth(dataset, task, args) 

if __name__ == '__main__':
    main()

