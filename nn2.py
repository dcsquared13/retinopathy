__author__ = 'dan'

import numpy as np
import pandas as pd
from sklearn.preprocessing import LabelEncoder
from sklearn.preprocessing import StandardScaler
import lasagne.layers as layers
from lasagne.nonlinearities import softmax, rectify
from lasagne.updates import nesterov_momentum
from nolearn.lasagne import NeuralNet
from nolearn.lasagne import BatchIterator
import cPickle as pickle
import sys
import os
import cv2
import theano
import numpy.random as nrandom
import skimage
from skimage import transform

PIXELS = 96


class DataAugmentationBatchIterator(BatchIterator):

    def transform(self, Xb, yb):
        Xb, yb = super(DataAugmentationBatchIterator, self).transform(Xb, yb)

        augmentation_params = {
            'zoom_range': (1.0, 1.1),
            'rotation_range': (0, 360),
            'shear_range': (0, 20),
            'translation_range': (-4, 4),
        }

        IMAGE_WIDTH = PIXELS
        IMAGE_HEIGHT = PIXELS

        def fast_warp(img, tf, output_shape=(PIXELS, PIXELS), mode='nearest'):
            """
            This wrapper function is about five times faster than skimage.transform.warp, for our use case.
            """
            #m = tf._matrix
            m = tf.params
            img_wf = np.empty((output_shape[0], output_shape[1]), dtype='float32')
            #for k in xrange(1):
            #    img_wf[..., k] = skimage.transform._warps_cy._warp_fast(img[..., k], m, output_shape=output_shape, mode=mode)
            img_wf = skimage.transform._warps_cy._warp_fast(img, m, output_shape=output_shape, mode=mode)
            return img_wf

        def random_perturbation_transform(zoom_range, rotation_range, shear_range, translation_range, do_flip=True):
            # random shift [-10, 10] - shift no longer needs to be integer!
            shift_x = np.random.uniform(*translation_range)
            shift_y = np.random.uniform(*translation_range)
            translation = (shift_x, shift_y)

            # random rotation [0, 360]
            rotation = np.random.uniform(*rotation_range) # there is no post-augmentation, so full rotations here!

            # random shear [0, 20]
            shear = np.random.uniform(*shear_range)

            # random zoom [0.9, 1.1]
            # zoom = np.random.uniform(*zoom_range)
            log_zoom_range = [np.log(z) for z in zoom_range]
            zoom = np.exp(np.random.uniform(*log_zoom_range)) # for a zoom factor this sampling approach makes more sense.
            # the range should be multiplicatively symmetric, so [1/1.1, 1.1] instead of [0.9, 1.1] makes more sense.

            translation = (0,0)
            rotation = 0.0
            shear = 0.0
            zoom = 1.0

            rotate = np.random.randint(4)
            if rotate == 0:
                rotation = 0.0
            elif rotate == 1:
                rotation = 90.0
            elif rotate == 2:
                rotation = 180.0
            else:
                rotation = 270.0

            '''
            # only translate 40% of the cases
            trans =  np.random.randint(10)
            if trans == 0:
                translation = (-2,-2)
            elif trans == 1:
                translation = (2,2)
            else:
                translation = (0,0)
            '''

            '''
            zooming =  np.random.randint(3)
            if zooming == 0:
                shear = 0
            elif zooming == 1 and rotate == 0:
                shear = 10
            elif zooming ==2 and rotate == 0:
                shear = 20
            else:
                shear = 0
            '''

            '''
            trans =  np.random.randint(5)
            if trans == 0:
                translation = (0,0)
            elif trans == 1:
                translation = (-4,0)
            elif trans == 2:
                translation = (0,-4)
            elif trans == 3:
                translation = (4,0)
            else:
                translation = (0,4)

            rotate =  np.random.randint(8)
            if rotate == 0:
                rotation = 0.0
            elif rotate == 1:
                rotation = 90.0
            elif rotate == 2:
                rotation = 180.0
            elif rotate == 3:
                rotation = 45.0
            elif rotate == 4:
                rotation = 135.0
            elif rotate == 5:
                rotation = 225.0
            elif rotate == 6:
                rotation = 315.0
            else:
                rotation = 270.0
            '''

            ## flip
            if do_flip and (np.random.randint(2) > 0): # flip half of the time
                shear += 180
                rotation += 180
                # shear by 180 degrees is equivalent to rotation by 180 degrees + flip.
                # So after that we rotate it another 180 degrees to get just the flip.

            '''
            print "translation = ", translation
            print "rotation = ", rotation
            print "shear = ",shear
            print "zoom = ",zoom
            print ""
            '''

            return build_augmentation_transform(zoom, rotation, shear, translation)


        center_shift   = np.array((IMAGE_HEIGHT, IMAGE_WIDTH)) / 2. - 0.5
        tform_center   = transform.SimilarityTransform(translation=-center_shift)
        tform_uncenter = transform.SimilarityTransform(translation=center_shift)

        def build_augmentation_transform(zoom=1.0, rotation=0., shear=0., translation=(0, 0)):
            tform_augment = transform.AffineTransform(scale=(1/zoom, 1/zoom),
                                                      rotation=np.deg2rad(rotation),
                                                      shear=np.deg2rad(shear),
                                                      translation=translation)
            # shift to center, augment, shift back (for the rotation/shearing)
            tform = tform_center + tform_augment + tform_uncenter
            return tform

        tform_augment  = random_perturbation_transform(**augmentation_params)
        tform_identity = skimage.transform.AffineTransform()
        tform_ds       = skimage.transform.AffineTransform()

        for i in range(Xb.shape[0]):
            new = fast_warp(Xb[i][0], tform_ds + tform_augment + tform_identity, output_shape=(PIXELS,PIXELS), mode='nearest').astype('float32')
            Xb[i,:] = new

        return Xb, yb


class AdjustVariable(object):
    def __init__(self, name, start=0.03, stop=0.001):
        self.name = name
        self.start, self.stop = start, stop
        self.ls = None

    def __call__(self, nn, train_history):
        if self.ls is None:
            self.ls = np.linspace(self.start, self.stop, nn.max_epochs)

        epoch = train_history[-1]['epoch']
        new_value = float32(self.ls[epoch - 1])
        getattr(nn, self.name).set_value(new_value)


class EarlyStopping(object):
    def __init__(self, patience=100):
        self.patience = patience
        self.best_valid = np.inf
        self.best_valid_epoch = 0
        self.best_weights = None

    def __call__(self, nn, train_history):
        current_valid = train_history[-1]['valid_loss']
        current_epoch = train_history[-1]['epoch']
        if current_valid < self.best_valid:
            self.best_valid = current_valid
            self.best_valid_epoch = current_epoch
            self.best_weights = nn.get_all_params_values()
        elif self.best_valid_epoch + self.patience < current_epoch:
            print("Early stopping.")
            print("Best valid loss was {:.6f} at epoch {}.".format(
                self.best_valid, self.best_valid_epoch))
            nn.load_params_from(self.best_weights)
            raise StopIteration()


def float32(k):
    return np.cast['float32'](k)


def shuffle_in_unison(a, b):
    rng_state = nrandom.get_state()
    nrandom.shuffle(a)
    nrandom.set_state(rng_state)
    nrandom.shuffle(b)
    return a, b


def mylistdir(directory):
    """A specialized version of os.listdir() that ignores files that
    start with a leading period."""
    filelist = os.listdir(directory)
    return [x for x in filelist
            if not (x.startswith('.'))]


def load_train_data(datafile, datadir):
    f = open(datafile)
    lines = []
    for i, l in enumerate(f, 1):
        lines.append(l)

    for j in range(1, i):
        filename, level = lines[j].split(',')
        print "Processing train image {}: {}".format(j, filename)
        img = cv2.imread(datadir + "/" + filename + ".jpeg").astype(np.float32)
        img_r = cv2.resize(img, (PIXELS, PIXELS))
        if j == 1:
            X = np.zeros((i-1, img_r.shape[2], img_r.shape[0], img_r.shape[1]))
            labels = np.empty((i-1), dtype=np.int32)
        img_r = np.asarray(img_r, dtype = 'float32') / 255.
        img_r = img_r.transpose(2,0,1).reshape(3, PIXELS, PIXELS)
        X[j-1] = img_r
        labels[j-1] = level.rstrip('\n')
    f.close()
    encoder = LabelEncoder()
    y = encoder.fit_transform(labels).astype(np.int32)
#    scaler = StandardScaler()
#    X = scaler.fit_transform(X)
    return X, y, encoder


def load_test_data(datafile, datadir):
    f = open(datafile)
    lines = []
    for i, l in enumerate(f, 1):
        lines.append(l)

    for j in range(1, i):
        filename = lines[j].rstrip('\n')
        print "Processing test image {}: {}".format(j, filename)
        img = cv2.imread(datadir + "/" + filename + ".jpeg").astype(np.float32)
        img_r = cv2.resize(img, (PIXELS, PIXELS))
        if j == 1:
            X = np.zeros((i-1, img_r.shape[2], img_r.shape[0], img_r.shape[1]))
            ids = np.empty((i-1), dtype=object)
        img_r = np.asarray(img_r, dtype = 'float32') / 255.
        img_r = img_r.transpose(2,0,1).reshape(3, PIXELS, PIXELS)
        X[j-1] = img_r
        ids[j-1] = filename
    f.close()
    return X, ids


def make_submission(ids, preds, name='my_neural_net_submission.csv'):
    with open(name, 'w') as f:
        f.write('image,level')
        f.write('\n')
        for id, pred in zip(ids, preds):
            f.write(id + "," + str(pred))
            f.write('\n')
    print "Wrote submission to file {}.".format(name)


def write_preds(preds, fname):
    pd.DataFrame({"ImageId": range(1,len(preds)+1), "Label": preds}).to_csv(fname, index=False, header=True)

sys.setrecursionlimit(10000)
train_files = '/Users/dan/Google Drive/kaggle/retinopathy/train'
test_files = '/Users/dan/Google Drive/kaggle/retinopathy/test'

print "Getting data"
X, y, encoder = load_train_data('csv/trainLabels.csv', train_files)
print "X len: {}".format(len(X))
print "y len: {}".format(len(y))

X_test, ids = load_test_data('csv/testLabels.csv', test_files)
print "Getting number of classes and features"

num_classes = len(encoder.classes_)
num_features = X.shape[1]

# shuffle X and y
X, y = shuffle_in_unison(X, y)

print "Number of classes: {}".format(num_classes)
print "Number of features: {}".format(num_features)

net0 = NeuralNet(
    layers=[
        ('input', layers.InputLayer),
        ('conv1', layers.Conv2DLayer),
        ('pool1', layers.MaxPool2DLayer),
        ('dropout1', layers.DropoutLayer),
        ('conv2', layers.Conv2DLayer),
        ('pool2', layers.MaxPool2DLayer),
        ('dropout2', layers.DropoutLayer),
        ('conv3', layers.Conv2DLayer),
        ('pool3', layers.MaxPool2DLayer),
        ('dropout3', layers.DropoutLayer),
        ('hidden1', layers.DenseLayer),
        ('dropout4', layers.DropoutLayer),
        ('hidden2', layers.DenseLayer),
        ('dropout5', layers.DropoutLayer),
        ('output', layers.DenseLayer),
        ],

    input_shape=(None, 3, PIXELS, PIXELS),

    conv1_num_filters=16, conv1_filter_size=(7, 7), pool1_pool_size=(2, 2),
    dropout1_p=0.2,
    conv2_num_filters=32, conv2_filter_size=(5, 5), pool2_pool_size=(2, 2),
    dropout2_p=0.2,
    conv3_num_filters=64, conv3_filter_size=(3, 3), pool3_pool_size=(2, 2),
    dropout3_p=0.3,
    hidden1_num_units=1024,
    dropout4_p=0.5,
    hidden2_num_units=1024,
    dropout5_p=0.5,

    output_num_units=num_classes,
    output_nonlinearity=softmax,

    update_learning_rate=theano.shared(float32(0.03)),
    update_momentum=theano.shared(float32(0.9)),
    batch_iterator_train=DataAugmentationBatchIterator(batch_size=128),
    regression=False,
    on_epoch_finished=[
        AdjustVariable('update_learning_rate', start=0.03, stop=0.001),
        AdjustVariable('update_momentum', start=0.9, stop=0.999),
        EarlyStopping(patience=30),
    ],
    max_epochs=400,
    verbose=1,
    eval_size=0.2,
)

net1 = NeuralNet(
    layers=[  # three layers: one hidden layer
        ('input', layers.InputLayer),
        ('hidden1', layers.DenseLayer),
        ('output', layers.DenseLayer),
        ],
    # layer parameters:
    input_shape=(None, 3, PIXELS, PIXELS),
    hidden1_num_units=2048,  # number of units in hidden layer
    output_nonlinearity=softmax,  # output layer uses identity function
    output_num_units=num_classes,

    # optimization method:
    update=nesterov_momentum,
    update_learning_rate=theano.shared(float32(0.01)),
    update_momentum=theano.shared(float32(0.9)),
    on_epoch_finished=[
        AdjustVariable('update_learning_rate', start=0.01, stop=0.001),
        AdjustVariable('update_momentum', start=0.9, stop=0.999),
        EarlyStopping(patience=25),
    ],
    regression=False,  # flag to indicate we're dealing with regression problem
    max_epochs=400,  # we want to train this many epochs
    eval_size=0.2,
    verbose=1,
    )

print "Running NN fit"

print "X shape: {}".format(X.shape)
print "Y shape: {}".format(y.shape)
print "Y: {}".format(y)
net1.fit(X, y)

predictions = net1.predict(X_test)

print "Predictions: {}".format(predictions)

print "Make submission"
make_submission(ids, predictions)

print "Pickling..."
with open('net1.pickle', 'wb') as f:
    pickle.dump(net0, f, -1)

