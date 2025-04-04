import networkx as nx
import numpy as np
import random
import re 
from anneal import Annealer
from centroid_anneal import CustomCentroidAnnealer
import sys, os
import json 
import multiprocessing
import pickle
import scipy.sparse as sp
from collections import defaultdict
# import cupy as cp
# import cupyx as cpx
# import cupy.sparse as cpsp
import torch

# import simanneal_centroid_tests as tests
import simanneal_centroid_helpers as helpers

# DIRECTORY = '/home/ilshapiro/project'
DIRECTORY = '/Users/ilanashapiro/Documents/constraints_project/project'
sys.path.append(DIRECTORY)
import build_graph

'''
Simulated Annealing (SA) Combinatorial Optimization Approach
1. Use SA to find optimal alignments between current centroid and each graph in corpus
2. Use the resulting average difference adjacency matrix between centroid and corpus to select the best (valid) transform. 
3. Modify centroid and repeat until loss converges. Loss is sum of dist from centroid to seach graph in corpus
'''

# chooose numpy, cupy, or Torch version depending on the architecture we're running this on
def align_numpy(P, A_G): # this is actually scipy for better performance but it's compatible with numpy
	P_sparse = sp.csr_matrix(P) # Conovert to sparse format
	A_G_sparse = sp.csr_matrix(A_G)
	return P_sparse.T @ A_G_sparse @ P_sparse # Perform efficient sparse matrix multiplication

def align_cupy(P, A_G):
	P_sparse = cpsp.csr_matrix(P) # Convert CuPy arrays to sparse matrices
	A_G_sparse = cpsp.csr_matrix(A_G)
	result = P_sparse.T @ A_G_sparse @ P_sparse
	return result.toarray() # Convert to dense

def align_torch(P, A_G):
	P_sparse = P.to_sparse()
	A_G_sparse = A_G.to_sparse()
	result = torch.sparse.mm(torch.sparse.mm(P_sparse.t(), A_G_sparse), P_sparse)
	return result.to_dense()


'''
Equation 1 in the paper
'''
# dist between g and G given alignment a
# i.e. reorder nodes of G according to alignment (i.e. permutation matrix) a
# ||A_g - a^t * A_G * a|| where ||.|| is the norm (using Frobenius norm)
# chooose numpy, cupy, or Torch version depending on the architecture we're running this on
def dist_numpy(A_g, A_G):
	return np.linalg.norm(A_g - A_G, 'fro')

def dist_cupy(A_g, A_G):
	return cp.linalg.norm(A_g - A_G, 'fro')

def dist_torch(A_g, A_G):
	return torch.norm(A_g - A_G, p='fro')

'''
This class contains the code for the simulated annealing procedure to compute the graph edit distance, from Section 4.2 of the paper
The output is the optimal alignment between 2 graphs, which allows us to directly compute the structural distance (which is square root edit distance under optimal alignment)
'''
class GraphAlignmentAnnealer(Annealer):
	def __init__(self, initial_alignment, A_g, A_G, centroid_idx_node_mapping, node_metadata_dict, device=None):
		super(GraphAlignmentAnnealer, self).__init__(initial_alignment)
		self.A_g = A_g
		self.A_G = A_G
		self.centroid_idx_node_mapping = centroid_idx_node_mapping
		self.node_metadata_dict = node_metadata_dict
		self.node_partitions = self.get_node_partitions()
		self.device = device
		
	# this prevents us from printing out alignment annealing updates since this gets confusing when also doing centroid annealing
	def default_update(self, step, T, E, acceptance, improvement):
		return 
	
	# return: (partition name, sub-level (if any))
	# partition name is the layer for instance nodes
	def get_node_partition_info(self, node_id):
		def get_layer_id(node_id):
			for layer_id in ['S', 'P', 'K', 'C', 'M']:
				if node_id.startswith(layer_id):
					return layer_id
			raise Exception("Invalid node", node_id)
		
		if node_id.startswith('Pr'): # prototype nodes: one partition per prototype feature
			# EVEN THOUGH IT'S POSOSIBLE FOR THE BEST ALIGNMENT TO MIX ACROSS FEATURE SETS OF THE SAME LEVEL, FOR LARGER GRAPHS THIS IS HIGHLY UNLIKELY
			# EXPERIMENTAL RESULTS SHOW GREATER ACCURACY BY PARTITIONING PROTOS BY FEATURE SETS, INSTEAD OF MERGED SOURCE LAYER SETS, DUE TO THIS UNLIKELIHOOD AND THE INCREASED 
			# ANNEALING EFFICIENCY ACHIEVED BY THE SMALLER PARTITIONS
			feature = self.node_metadata_dict[node_id]['feature_name']
			feature = feature if 'filler' not in feature else get_layer_id(node_id)
			return ('proto_' + feature, None)
			# VERSION WITH MERGED FEATURES INTO SOURCE LAYER PARTITIONS
			# 	source_layer_kind = self.node_metadata_dict[node_id]['source_layer_kind']
			# 	return ('proto_' + source_layer_kind, None)
		else: # instance nodes: one partition per layer kind e.g. P or S or C etc (fillers of that layer are included)
			layer_id = get_layer_id(node_id)
			hierarchical_layers = ['S']
			if layer_id in hierarchical_layers:
				sublevel = re.search(r'L(\d+)', node_id).group(1)
				return ("inst_" + layer_id, sublevel)
			return ("inst_" + layer_id, None)
	
	def get_node_partitions(self):
		"""Partition centroid_idx_node_mapping into labeled sets."""
		partitions = {}
		for index, node_id in self.centroid_idx_node_mapping.items():
			partition_name, layer = self.get_node_partition_info(node_id)
			hierarchical_layers = ['inst_S']

			if partition_name not in partitions:
					partitions[partition_name] = {} if partition_name in hierarchical_layers else []
	
			if partition_name == 'inst_S': # segmentation possibly has a sub-hierarchy
				if layer not in partitions[partition_name]:
					partitions[partition_name][layer] = []
				partitions[partition_name][layer].append(index)
			else:
				partitions[partition_name].append(index)
		
		return partitions

	def move(self):
		"""Swaps two rows in the n x n permutation matrix by permuting within valid sets (protype node class or individual level)"""
		n = len(self.state)
		i = random.randint(0, n - 1)
		i_partition_name, i_sublevel = self.get_node_partition_info(self.centroid_idx_node_mapping[i])
		j_options = None 
		hierarchical_layers = ['inst_S']

		# Identify partition and find a random j within the same partition
		if i_partition_name in hierarchical_layers and i_sublevel in self.node_partitions[i_partition_name]:
			j_options = self.node_partitions[i_partition_name][i_sublevel]
		elif i_partition_name:
			j_options = self.node_partitions[i_partition_name]

		# Ensure i is not equal to j
		if j_options and len(j_options) > 1: # if a partition has only 1 element we have infinite loop
			j = random.choice(j_options)
			while j == i: 
				j = random.choice(j_options)
		else:
			# Fallback to random selection if no suitable j is found
			j = random.randint(0, n - 1)
			while j == i:
				j = random.randint(0, n - 1)
		self.state[[i, j], :] = self.state[[j, i], :]  # Swap rows i and j

	def energy(self): # i.e. cost, self.state represents the permutation/alignment matrix P
		e = dist_torch(self.A_g, align_torch(self.state, self.A_G))
		# print("ENERGY", e)
		return e

'''
For running the nested Graph Alignment Annealer at each step of the Centroid Annealing
'''
def get_alignments_to_centroid(A_g, listA_G, idx_node_mapping, node_metadata_dict, device=None, Tmax=2, Tmin=0.01, steps=2000):
	alignments = []
	losses = []
	for i, A_G in enumerate(listA_G): # for each graph in the corpus, find its best alignment with current centroid
		# initial_state = cp.eye(cp.shape(A_G)[0]) # initial state is identity means we're doing the alignment with whatever A_G currently is
		initial_state = torch.eye(A_G.shape[0], dtype=torch.float64, device=device)

		graph_aligner = GraphAlignmentAnnealer(initial_state, A_g, A_G, idx_node_mapping, node_metadata_dict, device=device)
		graph_aligner.Tmax = Tmax
		graph_aligner.Tmin = Tmin
		graph_aligner.steps = steps
		# each time we make the new alignment annealer at each step of the centroid annealer, we want to UPDATE THE TEMPERATURE PARAM (decrement it at each step)
		# and can try decreasing number of iterations each time as well
		alignment, loss = graph_aligner.anneal() # don't do auto scheduling, it does not appear to work at all
		alignments.append(alignment)
		losses.append(loss.item())

	return alignments, np.mean(losses)

'''
The loss function for the centroid annealer (Equation 4a in paper) 
'''
# current centroid g, list of alignments list_a to the graphs in the corpus list_G
# loss is the sum of the distances between current centroid g and each graph in corpus G,
	# based on the current alignments
# this is our objective we're trying to minimize
# chooose numpy, cupy, or Torch version depending on the architecture we're running this on
# A large energy positive difference means very low acceptance probability (i.e. we don't want to accept a very bad state)
# vs small positive energy difference has higher acceptance probability
def loss_numpy(A_g, list_alignedA_G):
	distances = np.array([dist_numpy(A_g, A_G) for A_G in list_alignedA_G])
	return np.mean(distances) 

def loss_cupy(A_g, list_alignedA_G):
	distances = cp.array([dist_cupy(A_g, A_G) for A_G in list_alignedA_G])
	return cp.mean(distances) 

def loss_torch(A_g, list_alignedA_G, device):
	distances = torch.tensor([dist_torch(A_g, A_G) for A_G in list_alignedA_G], device=device)
	return torch.mean(distances)

'''
This class contains the code for the bi-level simulated annealing (SA) procedure from Section 5.1 of the paper
'''
class CentroidAnnealer(CustomCentroidAnnealer):
	def __init__(self, initial_centroid, listA_G, centroid_idx_node_mapping, node_metadata_dict, device=None):
		super(CentroidAnnealer, self).__init__(initial_centroid) # i.e. set initial self.state = initial_centroid
		self.listA_G = listA_G
		self.centroid_idx_node_mapping = centroid_idx_node_mapping
		self.node_metadata_dict = node_metadata_dict
		self.step = 0
		self.k_rejected_moves_since_last_accept = []
		self.last_accepted_move = None
		self.prev_move = None
		self.device = device

	# this prevents us from printing out annealing updates 
	# def default_update(self, step, T, E, acceptance, improvement):
	# 	return 
	
	'''
	Global constraints from Table 1 in the paper
	'''
	# i.e. the move always makes the score worse, it's not an intermediate invalid state that could lead to a better valid state
	def is_globlly_invalid_move(self, source_idx, sink_idx, node_mapping):
		# No self-loops (there would be a self-loop if we flip this coordinate)
		if source_idx == sink_idx and self.state[source_idx, sink_idx] == 0:
			return True
		
		source_node_id = node_mapping[source_idx]
		sink_node_id = node_mapping[sink_idx]

		def is_proto(node_id):
			return node_id.startswith('Pr')
		
		# The edge is from an instance to a prototype 
		if not is_proto(source_node_id) and is_proto(sink_node_id):
			return True
		
		# The edge is between two prototypes
		if is_proto(source_node_id) and is_proto(sink_node_id):
			return True
		
		# The edge is from a prototype to an instance level whose nodes don't have that prototype feature (i.e. PrAbs_interval -> segmentation)
		if is_proto(source_node_id) and not is_proto(sink_node_id):
			source_proto_feature = self.node_metadata_dict[source_node_id]['feature_name']
			sink_inst_features = self.node_metadata_dict[sink_node_id]['features_dict'].keys()
			if source_proto_feature not in sink_inst_features:
				return True
		
		# Source/sink are both instance
		if not is_proto(source_node_id) and not is_proto(sink_node_id):
			def rank_difference(rank1, rank2):
				primary_rank1, secondary_rank1 = rank1
				primary_rank2, secondary_rank2 = rank2
				if primary_rank1 == primary_rank2:
					return secondary_rank1 - secondary_rank2
				return primary_rank1 - primary_rank2
			
			# source level is NOT one level higher (i.e. 1 rank lower) or is NOT the same level than sink level
			# NOTE: this ONLY works for when we have a fixed number of levels in the graph. if sub-hierarchies are variable levels, then it's totally possible
			# to have an intermediate valid move of higher source->lower sink level that's not adjacent, like if we're in the process of deleting a level
			# but if all graphs have the same number of levels, like with flat segmentation or scluster, this won't happen, and hence we can add this important optimization
			# SO THIS MEANS WE DO NOT SUPPORT VARIABLE LEVEL SUB-HIERARCHIES
			# want difference = 0 (i.e. same source/sink level) or -1 (i.e. source level is one above sink level. higher level means lower rank value)
			source_rank = self.node_metadata_dict[source_node_id]['layer_rank']
			sink_rank = self.node_metadata_dict[sink_node_id]['layer_rank']
			if rank_difference(source_rank, sink_rank) not in [0, -1]:
				return True
		
		return False
	
	'''
	This function contains the logic for the move at each step of the simulated annealing, as explicated in detail in Section 5.1 (particularly, in Algorithm 2) in the paper
	We make 1 change to the centroid (i.e. self.state) at each step of the move
	'''
	def move(self):
		diff_matrices = torch.abs(torch.stack([self.state - A_G for A_G in self.listA_G]))
		score_matrix = torch.sum(diff_matrices, dim=0)

		# Flatten the score matrix. this is UNSORTED
		flat_scores = score_matrix.flatten()
		
		# flat_indices_sorted_by_score = cp.argsort(flat_scores)[::-1]
		flat_indices_sorted_by_score = torch.argsort(flat_scores, descending=True)

		valid_move_found = False
		batch_size = 100
		next_batch_start_index = 0 # index in flat_indices_sorted_by_score
		current_score = None
		# go through flat_indices_sorted_by_score in batches (batch num is num of unique scores)
		# since flat_indices_sorted_by_score is VERY VERY LARGE and takes way too long to go through all of it
		# while not valid_move_found and next_batch_start_index < len(flat_indices_sorted_by_score):
		while not valid_move_found and next_batch_start_index < len(flat_indices_sorted_by_score):
			print("next_batch_start_index", next_batch_start_index, "len(flat_indices_sorted_by_score)", len(flat_indices_sorted_by_score))
			batch_start_idx, batch_end_idx = next_batch_start_index, min(next_batch_start_index + batch_size, len(flat_indices_sorted_by_score))
			next_batch_start_index = batch_end_idx

			batch = flat_indices_sorted_by_score[batch_start_idx:batch_end_idx]
			batch = batch.cpu().numpy() # convert from tensor to CPU numpy so we can iterate through it

			# dict to group score matrix indices in the current batch by score
			score_index_mapping = defaultdict(list)
			for flat_index in batch:
				score = flat_scores[flat_index]
				score = score.item() # when score is a tensor we need to extract the value
				# print("FLAT INDEX", flat_index, "NEW  SCORE", score, "CURR SCORE", current_score, "NUM SCORES", len(score_index_mapping), "SCORES EQUAL", score == current_score)
				if score != current_score:
					current_score = score
				score_index_mapping[score].append(flat_index)
			# print("BATCH SCORES", score_index_mapping.items())

			unique_scores_descending = sorted(score_index_mapping.keys(), reverse=True) # highest -> lowest score
			# randomly shuffle the indices for each score partition, so we're trying a more variable set of moves that equally/most contribute to the loss
			# this helps the annear be less stuck and explore a wider variety of equally possible moves
			batch_flat_indices_sorted_by_score_and_shuffled = [] 
			for score in unique_scores_descending:
				indices = score_index_mapping[score]
				random.shuffle(indices) # Shuffle indices in the current score partition
				batch_flat_indices_sorted_by_score_and_shuffled.extend(indices)

			index_within_batch = 0
			while not valid_move_found and index_within_batch < len(batch_flat_indices_sorted_by_score_and_shuffled):
				flat_index = batch_flat_indices_sorted_by_score_and_shuffled[index_within_batch]
	
				# NOTE: uncomment the version (cp, torch, np) that matches the architecture we're running this on
				# coord = cp.unravel_index(flat_index, score_matrix.shape)
				# coord = torch.unravel_index(flat_index, score_matrix.shape)
				coord = np.unravel_index(flat_index, score_matrix.shape) # it's ok if score_matrix.shape is of type torch.shape bc this is still a tuple of ints
				# print("SCORE", flat_scores[flat_index])
				source_idx, sink_idx = coord
				source_idx, sink_idx = source_idx.item(), sink_idx.item() # for when source_idx, sink_idx are tensors, we need to unpack
				
				move_not_globally_invalid = not self.is_globlly_invalid_move(source_idx, sink_idx, self.centroid_idx_node_mapping)
				have_not_already_tried_move = coord not in self.k_rejected_moves_since_last_accept
				is_not_undoing_last_accept = coord != self.last_accepted_move
				
				if is_not_undoing_last_accept and have_not_already_tried_move and move_not_globally_invalid:
					valid_move_found = True
				else:
					index_within_batch += 1
			# print("BATCH ATTEMPT INDEX", index_within_batch)

		if valid_move_found:
			print("Flat index", flat_index)
			print("Coord", coord, "State at coord", self.state[source_idx, sink_idx])
			print("Most recent k=5 rejected moves since last accept", self.k_rejected_moves_since_last_accept)
			print("Last accepted move", self.last_accepted_move)
			self.state[source_idx, sink_idx] = 1 - self.state[source_idx, sink_idx] 
			self.prev_move = coord
			self.step += 1
		else:
			print("No valid move found.")

	'''
	This is the energy of the Centroid Annealer (Equation 5 in paper)
	We use the Graph Alignment Annealer to find the optimal alignments between current centroid and each STG in corpus (this is the nested simulated annealing step)
	Once we have the optimal alignments, we can compute the loss
	'''
	def energy(self): # i.e. cost, self.state represents the current centroid g
		current_temp_ratio = (self.T - self.Tmin) / (self.Tmax - self.Tmin)
		initial_Tmax = 1
		final_Tmax = 0.05
		initial_steps = 500
		final_steps = 5
		
		# Alignment annealer params Tmax and steps are dynamic based on the current temperature ratio for the centroid
		# They get narrower as we get an increasingly more accurate centroid that's easier to align
		alignment_Tmax = initial_Tmax * current_temp_ratio + final_Tmax * (1 - current_temp_ratio)
		alignment_steps = int(initial_steps * current_temp_ratio + final_steps * (1 - current_temp_ratio)) 
		
		# run the nested alignment annealer
		alignments, _ = get_alignments_to_centroid(self.state, self.listA_G, self.centroid_idx_node_mapping, self.node_metadata_dict, device=self.device, Tmax=alignment_Tmax, Tmin=0.01, steps=alignment_steps)

		# Align the corpus to the current centroid
		self.listA_G = list(map(align_torch, alignments, self.listA_G))
		l = loss_torch(self.state, self.listA_G, self.device) 
		print("LOSS", l)
		return l

if __name__ == "__main__":
	fp1 = DIRECTORY + '/datasets/beethoven/kunstderfuge/biamonti_461_(c)orlandi/biamonti_461_(c)orlandi_augmented_graph_ablation_5level_flat_RE.pickle'
	fp2 = DIRECTORY + '/datasets/beethoven/kunstderfuge/biamonti_811_(c)orlandi/biamonti_811_(c)orlandi_augmented_graph_ablation_5level_flat_RE.pickle'
	
	with open(fp1, 'rb') as f:
		G1 = pickle.load(f)
	with open(fp2, 'rb') as f:
		G2 = pickle.load(f)

	list_G = [G1, G2]
	listA_G, centroid_idx_node_mapping, node_metadata_dict = helpers.pad_adj_matrices(list_G)
	gpu_id = 0
	device = torch.device(f'cuda:{gpu_id}' if torch.cuda.is_available() else 'cpu')
	A_g, listA_G = torch.from_numpy(listA_G[0]).to(device), [torch.from_numpy(A_G).to(device) for A_G in listA_G[1:]] # A_g is initial centroid, at the front of the list
	
	test_dir = f"{DIRECTORY}/centroid/test_graph_output_files_beethoven_461_811"
	os.makedirs(test_dir, exist_ok=True)

	gen_centroid = False # FALSE FOR VISUALIZE EXISTING APPROX CENTORID ONLY, OTHERWISE TRUE TO GENERATE A NEW ONE
	if gen_centroid:
		alignments, _ = get_alignments_to_centroid(A_g, listA_G, centroid_idx_node_mapping, node_metadata_dict, device=device)
		for i, alignment in enumerate(alignments):
			file_name = f'{test_dir}/alignment_{i}.txt'
			np.savetxt(file_name, alignment.to(torch.int).cpu().numpy(), fmt='%i', delimiter=",")
			print(f'Saved: {file_name}')

		alignments = []
		for i in range(len(listA_G)):
			alignments.append(np.loadtxt(f'{test_dir}/alignment_{i}.txt', dtype=int, delimiter=","))
		alignments = [torch.tensor(alignment, device=device, dtype=torch.float64) for alignment in alignments]
		aligned_listA_G = list(map(align_torch, alignments, listA_G))

		centroid_annealer = CentroidAnnealer(A_g, aligned_listA_G, centroid_idx_node_mapping, node_metadata_dict, device=device)
		centroid_annealer.Tmax = 2.5
		centroid_annealer.Tmin = 0.05 
		centroid_annealer.steps = 1000
		centroid, min_loss = centroid_annealer.anneal()
		centroid = centroid.to(torch.int).cpu().numpy()

		centroid, centroid_idx_node_mapping = helpers.remove_unnecessary_dummy_nodes(centroid, centroid_idx_node_mapping, node_metadata_dict)
		
		np.savetxt(f"{test_dir}/approx_centroid_test.txt", centroid)
		print(f'Saved: {test_dir}/approx_centroid_test.txt')
		with open(f"{test_dir}/approx_centroid_idx_node_mapping_test.txt", 'w') as file:
			json.dump(centroid_idx_node_mapping, file)
		print(f'Saved: {test_dir}/approx_centroid_idx_node_mapping_test.txt')
		with open(f"{test_dir}/approx_centroid_node_metadata_test.txt", 'w') as file:
			json.dump(node_metadata_dict, file)
		print(f'Saved: {test_dir}/approx_centroid_node_metadata_test.txt')
		print(f"Best centroid", centroid)
		print(f"Best loss", min_loss)
		sys.exit(0)

	centroid = np.loadtxt(test_dir + "/approx_centroid_test.txt")
	
	with open(test_dir + "/approx_centroid_idx_node_mapping_test.txt", 'r') as file:
		centroid_idx_node_mapping = {int(k): v for k, v in json.load(file).items()}
	
	with open(test_dir + "/approx_centroid_node_metadata_test.txt", 'r') as file:
		node_metadata_dict = json.load(file)
	
	centroid, centroid_idx_node_mapping = helpers.remove_all_dummy_nodes(centroid, centroid_idx_node_mapping)

	g = helpers.adj_matrix_to_graph(centroid, centroid_idx_node_mapping, node_metadata_dict)
	
	# layers_G1 = build_graph.get_unsorted_layers_from_graph_by_index(G1)
	# layers_G2 = build_graph.get_unsorted_layers_from_graph_by_index(G2)
	layers_g = build_graph.get_unsorted_layers_from_graph_by_index(g)
	# build_graph.visualize([G2], [layers_G2], augmented=True)
	build_graph.visualize([g], [layers_g], augmented=True, compress_graph=False, ablation_level=2)