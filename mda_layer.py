__author__ = 'dowling'

import numpy as np
import logging

ln = logging.getLogger("mDA")
ln.setLevel(logging.DEBUG)

from scipy import sparse
from scipy.sparse import vstack, csc_matrix, csr_matrix

from gensim import utils, matutils


class FilteringDualGrouper(object):
    """
    Wrapper for simultaneously iterating over a corpus and its projection into a subset of dimensions.
    """
    def __init__(self, corpus, num_terms, filter_dimensions=None, chunksize=10000):
        self.corpus = corpus
        self.num_terms = num_terms
        self.filter_dimensions = filter_dimensions
        self.chunksize = chunksize

    def __iter__(self):
        for chunk_no, chunk in enumerate(utils.grouper(self.corpus, self.chunksize)):
            # ln.info("preparing a new chunk of documents")
            nnz = sum(len(doc) for doc in chunk)
            # construct the job as a sparse matrix, to minimize memory overhead
            # definitely avoid materializing it as a dense matrix!
            # ln.debug("converting corpus to csc format")
            job = matutils.corpus2csc(chunk, num_docs=len(chunk), num_terms=self.num_terms, num_nnz=nnz)

            if self.filter_dimensions is not None:
                filtered = job[self.filter_dimensions, :]
            else:
                filtered = None

            yield job, filtered
            del chunk


class mDALayer(object):
    def __init__(self, noise, lambda_, input_dimensionality, output_dimensionality=None, prototype_ids=None):
        self.noise = noise
        self.lambda_ = lambda_
        self.input_dimensionality = input_dimensionality
        if output_dimensionality is None or output_dimensionality == input_dimensionality:
            self.output_dimensionality = input_dimensionality
            # if we're not reducing, we don't need a random permutation as our target representation
            self.randomized_indices = range(self.input_dimensionality)
        else:
            if prototype_ids is None:
                ln.warn("Need prototype IDs to train reduction layer.")
            self.output_dimensionality = output_dimensionality
            self.randomized_indices = np.random.permutation(self.input_dimensionality)
        self.prototype_ids = prototype_ids

        self.num_folds = int(np.ceil(float(self.input_dimensionality) / self.output_dimensionality))
        self.blocks = []

    def train(self, corpus, chunksize=10000):
        if self.input_dimensionality != self.output_dimensionality:
            assert self.prototype_ids is not None, "Need prototype IDs to train dimensional reduction layer."

        if self.input_dimensionality != self.output_dimensionality:
            ln.info("mDA reduction layer with %s input and %s output dimensions is beginning training..",
                    self.input_dimensionality, self.output_dimensionality)
            ln.debug("Training the initial dimensional reduction with %s folds" % self.num_folds)
        else:
            ln.info("Training mDA layer with %s dimensions.", self.input_dimensionality)

        scatters_and_P_matrices = []
        # build scatter matrices
        ln.info("Building all scatter and P matrices (full corpus iteration).")
        processed = 0
        dualIterator = FilteringDualGrouper(corpus, self.input_dimensionality, self.prototype_ids, chunksize)
        for chunk_no, (doc_chunk, target_representation_chunk) in enumerate(dualIterator):

            for dim_batch_idx in range(self.num_folds):
                block_indices = self.randomized_indices[dim_batch_idx * self.output_dimensionality:
                                                        (dim_batch_idx + 1) * self.output_dimensionality]
                block_data = doc_chunk[block_indices]

                if len(scatters_and_P_matrices) <= dim_batch_idx:
                    scatter = np.zeros((len(block_indices) + 1, len(block_indices) + 1), dtype=float)

                    # we only explicitly construct P when we do dimensional reduction, otherwise we can use scatter
                    if target_representation_chunk is not None:
                        P = np.zeros((self.output_dimensionality, len(block_indices) + 1), dtype=float)
                    else:
                        P = None

                    scatters_and_P_matrices.append((scatter, P))
                else:
                    scatter, P = scatters_and_P_matrices[dim_batch_idx]

                blocksize = block_data.shape[1]
                bias = np.ones((1, blocksize))
                input_chunk = vstack((block_data, bias))

                scatter += input_chunk.dot(input_chunk.T)

                # we only explicitly construct P when we do dimensional reduction, otherwise we can use scatter
                if target_representation_chunk is not None:
                    #ln.debug("target: %s", target_representation_chunk.shape)
                    #ln.debug("input: %s", input_chunk.shape)
                    #ln.debug("update: %s", target_representation_chunk.dot(input_chunk.T).shape)
                    P += target_representation_chunk.dot(input_chunk.T)

            processed += blocksize

            ln.info("Processed %s chunks (%s documents)", chunk_no + 1, processed)

        if self.input_dimensionality != self.output_dimensionality:
            ln.info("Computing all reduction layer weights.")
        else:
            ln.info("Computing mDA layer weights.")

        for block_num, (scatter_matrix, P) in enumerate(scatters_and_P_matrices):
            if P is None:
                # we use scatter to compute P
                P = scatter_matrix.copy()
            P[:, :-1] *= (1 - self.noise)  # apply noise (except bias column)

            # P[:, self.input_dimensionality] *= (1.0 / (1 - self.noise))  # undo noise for bias column

            weights = self._computeWeights(scatter_matrix, P)
            if block_num % 10 == 9:
                ln.debug("layer trained up to fold %s/%s..", block_num + 1, self.num_folds)

            self.blocks.append(weights)
        if self.input_dimensionality != self.output_dimensionality:
            ln.info("mDA reduction layer completed training.")
        else:
            ln.info("mDA layer completed training.")

    def _computeWeights(self, scatter, P):
        r_dim = scatter.shape[0] - 1

        # DIMENSIONS OVERVIEW
        # scatter: always (d+1) x (d+1)
        # P: in normal mDA d x (d+1), else r x (d+1)
        # Q: same as scatter
        # W: in normal mDA d x (d+1), else r x (d+1)

        # we do the following in limited memory with a streamed corpus to more documents

        #ln.debug("Block input dim: %s. Output dim: %s" % (self.input_dimensionality, self.output_dimensionality))

        corruption = csc_matrix(np.ones((r_dim + 1, 1))) * (1 - self.noise)
        corruption[-1] = 1
        #ln.debug("corruption: %s, %s" % corruption.shape)

        # this is a hacky translation of the original Matlab code, to avoid allocating a big (d+1)x(d+1) matrix
        # instead of element-wise multiplying the matrices, we handle the corresponding areas individually

        # corrupt everything
        Q = scatter * (1-self.noise)**2
        # partially undo corruption to values in (d+1,:)
        Q[r_dim] = scatter[r_dim] * (1.0/(1-self.noise))
        # partially undo corruption to values in (:,d+1)
        Q[:, r_dim] = scatter[:, r_dim] * (1.0/(1-self.noise))
        # undo corruption of (-1, -1)
        Q[-1, -1] = scatter[-1, -1] * (1.0/(1-self.noise)**2)

        # replace the diagonal (this is according to the original code again)
        idxs = range(r_dim + 1)

        Q[idxs, idxs] = np.squeeze(np.asarray(np.multiply(corruption.todense().T, (scatter[idxs, idxs]))))

        reg = sparse.eye(r_dim + 1, format="csc").multiply(self.lambda_)

        reg[-1, -1] = 0

        # W is going to be dx(d+1) (or rx(d+1) for high dimensions)

        # we need to solve W = P * Q^-1
        # Q is symmetric, so Q = Q^T
        # WQ = P
        # (WQ)^T = P^T
        # Q^T W^T = P^T
        # Q W^T = P^T
        # thus, self.weights = np.linalg.lstsq((Q + reg), P.T)[0].T
        #ln.debug("solving for weights...")
        # Qreg = (Q + reg) # This is based on Q and reg, and is therefore symmetric
        weights = np.linalg.lstsq((Q + reg), P.T)[0].T

        del P
        del Q
        del scatter
        del corruption

        return weights

    @staticmethod
    def _get_intermediate_representations(block_weights, block_input_data):

        dimensionality, num_documents = block_input_data.shape

        bias = csc_matrix(np.ones((1, num_documents)))

        block_input_data = vstack((block_input_data, bias)).todense()

        hidden_representations = np.dot(block_weights, block_input_data)

        del block_input_data
        del bias

        return hidden_representations

    def _get_hidden_representations(self, input_data):
        representation_avg = None
        for dim_batch_idx in range(self.num_folds):
            block_indices = self.randomized_indices[dim_batch_idx * self.output_dimensionality:
                                                    (dim_batch_idx + 1) * self.output_dimensionality]
            block_data = input_data[block_indices]
            block_weights = self.blocks[dim_batch_idx]

            block_hidden = self._get_intermediate_representations(block_weights, block_data)
            if representation_avg is None:
                representation_avg = block_hidden
            else:
                representation_avg += (1.0 / (dim_batch_idx + 1)) * (block_hidden - representation_avg)
        representation_avg = np.tanh(representation_avg)

        return representation_avg

    def __getitem__(self, input_data, numpy_input=False, numpy_output=False, chunksize=10000):
        if numpy_input:
            if numpy_output:
                return self._get_hidden_representations(input_data)
            else:
                return matutils.any2sparse(self._get_hidden_representations(input_data))
        else:
            is_corpus, input_data = utils.is_corpus(input_data)
            if not is_corpus:
                input_data = [input_data]

            if chunksize:
                def transformed_corpus():
                    for doc_chunk in utils.grouper(input_data, chunksize):
                        chunk = matutils.corpus2dense(doc_chunk, self.input_dimensionality)
                        hidden = self._get_hidden_representations(chunk)
                        for column in hidden.T:
                            if numpy_output:
                                yield column
                            else:
                                yield matutils.any2sparse(column)

            else:
                def transformed_corpus():
                    for doc in input_data:
                        if numpy_output:
                            yield self._get_hidden_representations(matutils.corpus2dense(doc, self.input_dimensionality))
                        else:
                            yield matutils.any2sparse(
                                self._get_hidden_representations(matutils.corpus2dense(doc, self.input_dimensionality)))

            if not is_corpus:
                return list(transformed_corpus()).pop()
            else:
                return transformed_corpus()
