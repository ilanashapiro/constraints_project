from multiprocessing import Value
from os import remove
import networkx as nx
from collections import Counter
import random
import re

def node_subst_cost(attr_dict1, attr_dict2):
	if attr_dict1['label'] != attr_dict2['label']:
		return 1
	return 0

def is_invalid_proposal_type(t):
	# since we set edge substitution to be zero-cost, so subbing an edge can't possibly decreast the cost
	# substituting an edge should have cost 2, which is taken care of by 2 node substitutions 
	# we don't worry about the case where an edge direction is flipped, which could be an issue
	# since we're dealing with a directed graph, because all possible edge subs will be in the same direction
	# (i.e. pointing "down" the hierarchy) due to the structure of the graph
	is_edge_subst = (isinstance(t[0], tuple) and all(isinstance(sub_t, str) for sub_t in t[0]) and
									isinstance(t[1], tuple) and all(isinstance(sub_t, str) for sub_t in t[1]))
	
	# node substitutions where the labels are the same also give zero cost
	is_identical_node_subst = all(isinstance(sub_t, str) for sub_t in t) and t[0] == t[1]

	return is_edge_subst or is_identical_node_subst

def is_invalid_proposal_application(R_curr, t):
	is_node_del = isinstance(t[0], str) and t[1] is None 
	# only delete node if (total, i.e. in- and out-degree) arity is zero, otherwise we end up with 1-2 edge deletions as well
	nonzero_arity_node_del = is_node_del and R_curr.degree(t[0]) != 0
	nonexist_node_del = is_node_del and not R_curr.has_node(t[0])

	is_edge_del = isinstance(t[0], tuple) and t[1] is None
	nonexist_edge_del = is_edge_del and not R_curr.has_edge(t[0][0], t[0][1])

	return nonzero_arity_node_del or nonexist_node_del or nonexist_edge_del 

def combine_counters(c1, c2):
	result = Counter()
	all_keys = set(c1.keys()) | set(c2.keys())
	
	for key in all_keys:
		new_count = c1[key] + c2[key]
		result[key] = new_count # explicitly include zero counts
	
	return result

def get_transform_inverse(elem):
	return (elem[1], elem[0])

def build_transform_counts(transforms):
	transform_counts = Counter()
	for transform in transforms:
		if is_invalid_proposal_type(transform):
			continue
		
		transform_counts[transform] += 1
		# transform_counts[get_transform_inverse(transform)] += 0

	return transform_counts

# Additive/Laplace smoothing
# alpha of 1 corresponds to traditional Laplace smoothing, but any positive value can be used
def additive_smooth(transform_counts, alpha=0.01):
	smoothed_dict = {}
	total_count = sum(transform_counts.values())
	total_count_adjusted = total_count + alpha * len(transform_counts) 
	smoothed_dict = {transform: (count + alpha) / total_count_adjusted for transform, count in transform_counts.items()}
	return smoothed_dict

def generate_proposal(proposal_dist):
	transforms = list(proposal_dist.keys())
	weights = list(proposal_dist.values())
	return random.choices(transforms, weights, k=1)[0]

def remove_node_index(node_id):
	index_pattern = r'N\d+'
	return re.sub(index_pattern, '', node_id)

def apply_transform(R, t):
	match t:
		case (None, str(b)): # node insertion
			R.add_node(b, label=b)
			print("ADDING", b)
			print("CHECK", R.nodes[b])
		case (str(a), None): # node deletion
			R.remove_node(a)
		case (str(a), str(b)): # node substitution
			nx.relabel_nodes(R, {a:b}, copy=False) # change R directly in memory
			nx.set_node_attributes(R, {b: {'label': b}})
			for _, y in list(R.out_edges(b)):
				nx.set_edge_attributes(R, {(b, y): {'label': f"({b},{y})"}})
			for x, _ in list(R.in_edges(b)):
				nx.set_edge_attributes(R, {(x, b): {'label': f"({x},{b})"}})
		case (None, (str(a), str(b))): # edge insertion
			R.add_edge(a, b, label=f"({a},{b})")
			print("ADDING", (a, b))
			print("CHECK", R.edges[(a,b)])
			# After experimenting, it appears that optimize_edit_paths won't directly add an edge 
		case ((str(a), str(b)), None): # edge deletion
			R.remove_edge(a, b) # It's possible for us to end up with zero-arity nodes this way. Maybe this is ok since we have to validate anyways and fix later???
		case _:
			return ValueError("Invalid transform proposal application")
	return R