import numpy as np
import theano
import theano.tensor as T
import sys, random, pprint

from theano_util import *
from keras.activations import tanh, hard_sigmoid
from keras.initializations import glorot_uniform, orthogonal
from keras.utils.theano_utils import shared_zeros, alloc_zeros_matrix
from keras.preprocessing import sequence

from qa_dataset_parser import parse_qa_dataset

import cPickle

# theano.config.exception_verbosity = 'high'
# theano.config.allow_gc = False
#theano.config.profile = True

def inspect_inputs(i, node, fn):
    print i, node, "inputs:", [input[0] for input in fn.inputs],

def inspect_outputs(i, node, fn):
    print i, node, "outputs:", [output[0] for output in fn.outputs]

class WMemNN:
    def __init__(self, n_words=20, n_embedding=100, lr=0.01,
                 momentum=0.9, word_to_id=None, null_word_id=-1,
                 max_stmts=20, max_words=20, load_from_file=None):
        if load_from_file:
            self.load_model(load_from_file)
        else:
            self.regularization = 0.001
            self.n_embedding = n_embedding
            self.lr = lr
            self.momentum = momentum
            self.n_words = n_words
            self.batch_size = 4
            self.max_stmts = max_stmts
            self.max_words = max_words

            self.word_to_id = word_to_id
            self.id_to_word = dict((v, k) for k, v in word_to_id.iteritems())
            self.null_word_id = null_word_id

            # Question embedding
            # self.B = init_shared_normal(self.n_words, self.n_embedding, 0.1)

            # Statement input, output embeddings
            self.weights = init_shared_normal_tensor(4, self.n_words, self.n_embedding, 0.1)

            # Linear mapping between layers
            self.H = init_shared_normal(self.n_embedding, self.n_embedding, 0.1)

            # Final outut weight matrix
            # self.W = init_shared_normal(self.n_embedding, self.n_words, 0.1)


        zero_vector = T.vector('zv', dtype=theano.config.floatX)

        # Statement
        x = T.imatrix('x')
        xbatch = T.tensor3('xb', dtype='int32')

        # Positional encoding matrix
        pe = T.tensor3('pe')

        # Question
        q = T.ivector('q')
        qbatch = T.imatrix('qb')

        # True word
        r = T.iscalar('r')
        rbatch = T.ivector('rb')

        memory_cost = self.memnn_cost(x, q, pe)
        # memory_loss = -T.log(memory_cost[r]) # cross entropy on softmax
        memory_loss = self.memnn_batch_cost(xbatch, qbatch, rbatch, pe)

        params = [
            self.weights,
            # self.B,
            # self.W,
            self.H
        ]

        regularization_cost = reduce(
            lambda x,y: x + y,
            map(lambda x: self.regularization * T.sum(x ** 2), params)
        )

        cost = memory_loss + regularization_cost

        grads = T.grad(cost, params)

        l_rate = T.scalar('l_rate')

        # Parameter updates
        updates = get_param_updates(params, grads, lr=l_rate, method='momentum', momentum=0.9,
            constraint=self._constrain_embedding(self.null_word_id, zero_vector))

        self.train_function = theano.function(
            inputs = [
                xbatch, qbatch, rbatch, pe,
                theano.Param(l_rate, default=self.lr),
                theano.Param(zero_vector, default=np.zeros((self.n_embedding,), theano.config.floatX))
            ],
            outputs = cost,
            updates = updates,
            allow_input_downcast=True,
            # mode='FAST_COMPILE',
            #mode='DebugMode'
            #mode=theano.compile.MonitorMode(pre_func=inspect_inputs,post_func=inspect_outputs)
            on_unused_input='warn'
        )

        self.predict_function = theano.function(
            inputs = [
                x, q, pe
            ],
            outputs = memory_cost,
            allow_input_downcast=True,
            # mode='FAST_COMPILE',
            on_unused_input='warn'
        )

    def _constrain_embedding(self, null_id, zero_vector):
        def wrapper(p):
            for i in range(4):
                p = T.set_subtensor(p[i,null_id], zero_vector)
            return p
        return wrapper

    def _compute_memories(self, statement, previous, weights, pe_matrix):
        pe_weights = pe_matrix * weights[statement]
        memories = T.sum(pe_weights, axis=0)
        return memories

    def _get_PE_matrix(self, num_words, embedding_size):
        pe_matrix = np.ones((num_words, 4, embedding_size), theano.config.floatX)
        # for j in range(num_words):
        #     for k in range(embedding_size):
        #         value = (1 - float(j+1)/num_words) - (float(k+1)/embedding_size) * (1 - 2*float(j+1)/num_words)
        #         for i in range(4):
        #             pe_matrix[j,i,k] = value
        return pe_matrix

    def save_model(self, filename):
        f = file(filename, 'wb')
        for obj in [self.regularization, self.n_embedding, self.lr,
                    self.momentum, self.n_words, self.batch_size,
                    self.word_to_id, self.id_to_word, self.null_word_id,
                    self.max_stmts, self.max_words, self.weights, self.H]:
            cPickle.dump(obj, f, protocol=cPickle.HIGHEST_PROTOCOL)
        f.close()

    def load_model(self, filename):
        f = file(filename, 'rb')
        self.regularization = cPickle.load(f)
        self.n_embedding = cPickle.load(f)
        self.lr = cPickle.load(f)
        self.momentum = cPickle.load(f)
        self.n_words = cPickle.load(f)
        self.batch_size = cPickle.load(f)
        self.word_to_id = cPickle.load(f)
        self.id_to_word = cPickle.load(f)
        self.null_word_id = cPickle.load(f)
        self.max_stmts = cPickle.load(f)
        self.max_words = cPickle.load(f)
        self.weights = cPickle.load(f)
        self.H = cPickle.load(f)
        f.close()


    def memnn_batch_cost(self, statements_batch, question_batch, r_batch, pe_matrix):
        l = statements_batch.shape[0]
        s, _ = theano.scan(fn=lambda i, c, xb, qb, rb, pe: c - T.log(self.memnn_cost(xb[i], qb[i], pe)[rb[i]]),
                           outputs_info=T.as_tensor_variable(np.asarray(0, theano.config.floatX)),
                           non_sequences=[statements_batch, question_batch, r_batch, pe_matrix],
                           sequences=[theano.tensor.arange(l, dtype='int64')])
        return s[-1]

    def memnn_cost(self, statements, question, pe_matrix):
        # statements: list of list of word indices
        # question: list of word indices

        computed_memories, updates = theano.scan(
            self._compute_memories,
            sequences = [statements],
            outputs_info = [
                alloc_zeros_matrix(self.weights.shape[0], self.n_embedding)
            ],
            non_sequences = [
                self.weights.dimshuffle(1, 0, 2),
                pe_matrix
            ],
            truncate_gradient = -1,
        )

        memories = T.stacklists(computed_memories).dimshuffle(1, 0, 2)

        # Embed question
        u1 = T.sum(self.weights[0][question], axis=0)

        # Layer 1
        p = T.nnet.softmax(T.dot(u1, memories[0].T))
        o1 = T.dot(p, memories[1])

        # Layer 2
        u2 = o1 + T.dot(u1, self.H)
        p = T.nnet.softmax(T.dot(u2, memories[1].T))
        o2 = T.dot(p, memories[2])

        # Layer 3
        u3 = o2 + T.dot(u2, self.H)
        p = T.nnet.softmax(T.dot(u3, memories[2].T))
        o3 = T.dot(p, memories[3])

        # Final
        output = T.nnet.softmax(T.dot(o3 + u3, self.weights[3].T))

        return output[0]

    def train(self, dataset, questions, n_epochs=100, lr_schedule=None, start_epoch=0, max_words=20):
        l_rate = self.lr
        index_array = np.arange(len(questions))

        # (max_words, )
        pe_matrix = self._get_PE_matrix(max_words, self.n_embedding)

        for epoch in xrange(start_epoch, start_epoch + n_epochs):
            costs = []

            if lr_schedule != None and epoch in lr_schedule:
                l_rate = lr_schedule[epoch]

            np.random.shuffle(index_array)
            seen = 0

            batches = make_batches(len(questions), self.batch_size)
            for batch_index, (batch_start, batch_end) in enumerate(batches):
                batch_ids = index_array[batch_start:batch_end]
                seen += len(batch_ids)
                questions_batch = []
                for index in batch_ids:
                    questions_batch.append(questions[index])

                # (batch_size * max_stmts * max_words)
                statements_seq_batch = np.asarray(map(lambda x: x[2], questions_batch), theano.config.floatX)
                # (batch_size * max_words)
                question_seq_batch = np.asarray(map(lambda x: x[3], questions_batch), theano.config.floatX)
                # (batch_size)
                correct_word_batch = np.asarray(map(lambda x: x[4], questions_batch), theano.config.floatX)

                cost = self.train_function(
                    statements_seq_batch,
                    question_seq_batch,
                    correct_word_batch,
                    pe_matrix,
                    l_rate
                )

                # print "Epoch %d, sample %d: %f" % (epoch, i, cost)
                costs.append(cost)

            print "Epoch %d: %f" % (epoch, np.mean(costs))

    def predict(self, dataset, questions, max_words=20, print_errors=False):
        correct_answers = 0
        wrong_answers = 0
        pe_matrix = self._get_PE_matrix(max_words, self.n_embedding)

        for i, question in enumerate(questions):
            statements_seq = np.asarray(question[2], theano.config.floatX)
            question_seq = np.asarray(question[3], theano.config.floatX)
            correct = question[4]

            probs = self.predict_function(
                statements_seq, question_seq, pe_matrix
            )
            predicted = np.argmax(probs)

            if len(question) == 6:
                ## For mc_test
                options = question[5]
                options_probs = probs[options]
                best_idx = np.argmax(options_probs)
                predicted = options[best_idx]
                ##

            if predicted == correct:
                correct_answers += 1
            else:
                if print_errors and np.random.rand() < 0.02:
                    print 'Correct: %s (%d %.3f), Guess: %s (%d %.3f)' % (self.id_to_word[correct], correct, probs[correct], self.id_to_word[predicted], predicted, probs[predicted])
                wrong_answers += 1

            if len(questions) > 1000:
                print '(%d/%d) %d correct, %d wrong' % (i+1, len(questions), correct_answers, wrong_answers)

        print '%d correct, %d wrong' % (correct_answers, wrong_answers)

if __name__ == "__main__":
    train_file = sys.argv[1]
    test_file = train_file.replace('train', 'test')

    if len(sys.argv) > 2:
        n_epochs = int(sys.argv[2])
    else:
        n_epochs = 10

    if len(sys.argv) > 3:
        n_embedding = int(sys.argv[3])
    else:
        n_embedding = 20

    mode = 'babi' # babi or wiki

    if '.pickle' in train_file:
        mode = 'wiki'

    max_stmts = 20
    max_words = 20

    if mode == 'babi':
        train_dataset, train_questions, word_to_id, num_words, null_word_id = parse_dataset_weak(train_file, max_stmts=max_stmts, max_words=max_words)
        test_dataset, test_questions, _, _, _ = parse_dataset_weak(test_file, word_id=num_words, word_to_id=word_to_id, update_word_ids=False, max_stmts=max_stmts, max_words=max_words)
    elif mode == 'wiki':
        # Check for pickled dataset
        print("Loading pickled train dataset")
        f = file(train_file, 'rb')
        import cPickle
        obj = cPickle.load(f)
        train_dataset, train_questions, word_to_id, num_words, null_word_id = obj

        print("Loading pickled test dataset")
        f = file(test_file, 'rb')
        obj = cPickle.load(f)
        test_dataset, test_questions, _, _, _ = obj
    elif mode == 'debug':
        train_dataset = []
        train_questions = [[0, 2, [[0, 1, 2, 3, 4, 5], [6, 7, 2, 3, 8, 5], [9, 10, 0, 11]], 4]]
        num_words = 12
        word_to_id = {}

    print "Dataset has %d words" % num_words
    # print train_questions[0]

    model_file = "mctest500_dim100_wmemnn.pickle"
    train_my_model = False
    save_my_model = True

    if train_my_model:
        wmemNN = WMemNN(n_words=num_words, n_embedding=100, lr=0.01, word_to_id=word_to_id, null_word_id=null_word_id,
                        max_stmts=max_stmts, max_words=max_words)

        lr_schedule = dict([(0, 0.01), (25, 0.01/2), (50, 0.01/4), (75, 0.01/8)])

        for i in xrange(n_epochs/5):
            wmemNN.train(train_dataset, train_questions, 5, lr_schedule, 5*i, max_words)
            wmemNN.predict(train_dataset, train_questions, max_words)
            wmemNN.predict(test_dataset, test_questions, max_words)

        if save_my_model:
            print "Saving model to", model_file
            wmemNN.save_model(model_file)
    else:
        wmemNN = WMemNN(load_from_file=model_file)
        wmemNN.predict(train_dataset, train_questions, max_words)
        wmemNN.predict(test_dataset, test_questions, max_words)


