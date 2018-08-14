import cv2
import numpy as np
import pandas as pd
import os.path as osp
from skimage import segmentation
from skimage import morphology
from skimage import measure
from skimage import filters
from skimage import exposure
from skimage import transform
from skimage.future import graph as label_graph
from scipy import ndimage
from codex import math as codex_math
from codex import data as codex_data

DEFAULT_BATCH_SIZE = 1
CELL_CHANNEL = 0
NUCLEUS_CHANNEL = 1
DEFAULT_CELL_INTENSITY_PREFIX = 'ci:'
DEFAULT_NUCL_INTENSITY_PREFIX = 'ni:'
DEFAULT_CELL_GRAPH_PREFIX = 'cg:'


class KerasCytometer2D(object):

    def __init__(self, input_shape, target_shape=None, weights_path=None):
        """Cytometer Initialization

        Args:
            input_shape: Shape of input images as HWC tuple
            target_shape: Shape of resized images to use for prediction as HW tuple; if None (default),
                then the images will not be resized
            weights_path: Path to model weights; if None (default), a default path will be used
        """
        self.input_shape = input_shape
        self.target_shape = target_shape
        self.weights_path = weights_path
        self.initialized = False
        self.model = None

        if len(input_shape) != 3:
            raise ValueError('Input shape must be HWC 3 tuple (given {})'.format(input_shape))
        if target_shape is not None and len(target_shape) != 2:
            raise ValueError('Target shape must be HW 2 tuple (given {})'.format(target_shape))

        # Set resize indicator to true only if target HW dimensions differ from original
        self.resize = target_shape is not None and target_shape != input_shape[:2]

    def initialize(self):
        # Choose input shape for model based on whether or not resizing is being used
        if self.resize:
            # Set as HWC where HW comes from target shape
            input_shape = tuple(self.target_shape) + (self.input_shape[-1],)
        else:
            input_shape = self.input_shape
        self.model = self._get_model(input_shape)
        self.model.load_weights(self.weights_path or self._get_weights_path())
        self.initialized = True
        return self

    def _resize(self, img, shape):
        """Resize NHWC image to target shape

        Args:
            img: Image array with shape NHWC
            shape: Shape to resize to (HW tuple)
        Return:
            Image array with shape NHWC where H and W are equal to sizes in `shape`
        """
        if img.ndim != 4:
            raise ValueError('Expecting 4D NHWC image to resize (given shape = {})'.format(img.shape))
        if len(shape) != 2:
            raise ValueError('Expecting 2 tuple for target shape (given {})'.format(shape))

        # Resize expects images as HW first and then trailing dimensions will be ignored if
        # explicitly set to resize them to the same values
        input_shape = img.shape
        output_shape = tuple(shape) + (input_shape[-1], input_shape[0])
        img = np.moveaxis(img, 0, -1)  # NHWC -> HWCN
        img = transform.resize(img, output_shape=output_shape, mode='constant', anti_aliasing=True, preserve_range=True)
        img = np.moveaxis(img, -1, 0)  # HWCN -> NHWC

        # Ensure result agrees with original in N and C dimensions
        assert img.shape[0] == input_shape[0] and img.shape[-1] == input_shape[-1], \
            'Resized image does not have expected batch and channel dim values (input shape = {}, result shape = {}' \
            .format(input_shape, img.shape)
        return img

    def predict(self, img, batch_size):
        """Run prediction for an image

        Args:
            img: Image array with shape NHWC
            batch_size: Number of images to predict at one time
        Return:
            Predictions from model with shape NHWC where C can differ from input while all other dimensions are
                the same (difference depends on prediction targets of model)
        """
        if img.ndim != 4:
            raise ValueError('Expecting 4D NHWC image for prediction but got image with shape "{}"'.format(img.shape))
        if img.shape[1:] != self.input_shape:
            raise ValueError(
                'Given image with shape {} does not match expected image shape {} in non-batch dimensions'
                .format(img.shape[1:], self.input_shape)
            )
        if batch_size < 1:
            raise ValueError('Batch size must be integer >= 1 (given {})'.format(batch_size))

        shape = img.shape

        # Resize input, if necessary
        if self.resize:
            img = self._resize(img, self.target_shape)

        # Run predictions on NHWC0 image to give NHWC1 result where C0 possibly != C1
        img = self.model.predict(img, batch_size=batch_size)

        # Make sure results are NHWC
        if img.ndim != 4:
            raise AssertionError('Expecting 4D prediction image results but got image with shape {}'.format(img.shape))

        # Convert HW dimensions of predictions back to original, if necessary
        if self.resize:
            img = self._resize(img, self.input_shape[:2])

        # Ensure results agree with input in NHW dimensions
        assert img.shape[:-1] == shape[:-1], \
            'Prediction and input images do not have same NHW dimensions (input shape = {}, result shape = {})' \
            .format(shape, img.shape)

        return img

    def _get_model(self, input_shape):
        raise NotImplementedError()

    def _get_weights_path(self):
        raise NotImplementedError()


def _to_uint8(img, name):
    if img.dtype != np.uint8 and img.dtype != np.uint16:
        raise ValueError(
            'Image must be 8 or 16 bit for segmentation (image name = {}, dtype = {}, shape = {})'
            .format(name, img.dtype, img.shape)
        )
    if img.dtype == np.uint16:
        img = exposure.rescale_intensity(img, in_range=np.uint16, out_range=np.uint8).astype(np.uint8)
    return img


class ObjectProperties(object):

    def __init__(self, cell, nucleus):
        self.cell = cell
        self.nucleus = nucleus
        if cell.label != nucleus.label:
            raise ValueError(
                'Expecting equal labels for cell and nucleus (nucleus label = {}, cell label = {})'
                .format(nucleus.label, cell.label)
            )


class FeatureCalculator(object):

    def get_feature_names(self):
        raise NotImplementedError()

    def get_feature_values(self, signals, labels, graph, props, z):
        raise NotImplementedError()


class BasicCellFeatures(FeatureCalculator):

    def get_feature_names(self):
        # Note: "size" is used here instead of area/volume for compatibility between 2D and 3D
        return [
            'id', 'x', 'y', 'z',
            'cell_size', 'cell_diameter', 'cell_perimeter', 'cell_solidity',
            'nucleus_size', 'nucleus_diameter', 'nucleus_perimeter', 'nucleus_solidity'
        ]

    def get_feature_values(self, signals, labels, graph, props, z):
        # Extract these once as their calculations are not cached
        cell_area, nuc_area = props.cell.area, props.nucleus.area
        return [
            props.cell.label, props.cell.centroid[1], props.cell.centroid[0], z,
            cell_area, codex_math.area_to_diameter(cell_area), props.cell.perimeter, props.cell.solidity,
            nuc_area, codex_math.area_to_diameter(nuc_area), props.cell.nucleus, props.nucleus.solidity
        ]


def _quantify_intensities(image, prop):
    # Get a (n_pixels, n_channels) array of intensity values associated with
    # this region and then average across n_pixels dimension
    intensities = image[prop.coords[:, 0], prop.coords[:, 1]].mean(axis=0)
    assert intensities.ndim == 1, 'Expecting 1D resulting intensities but got shape {}'.format(intensities.shape)
    return list(intensities)


class IntensityFeatures(FeatureCalculator):

    def __init__(self, n_channels, prefix, component, channel_names=None):
        self.n_channels = n_channels
        self.prefix = prefix
        self.channel_names = channel_names
        self.component = component
        if component not in ['cell', 'nucleus']:
            raise ValueError(
                'Cellular component to quantify intensities for must be one of ["cell", "nucleus"] not "{}"'
                .format(component)
            )

    def get_feature_names(self):
        # Get list of raw channel names (with default to numbered list)
        if self.channel_names is None:
            channel_names = ['{:03d}'.format(i) for i in range(self.n_channels)]
        else:
            channel_names = self.channel_names

        return [self.prefix + c for c in channel_names]

    def get_feature_values(self, signals, labels, graph, props, z):
        # Signals should have shape ZHWC
        assert signals.ndims == 4, 'Expecting 4D signals image but got shape {}'.format(signals.shape)

        # Intentionally avoid using attribute inference / reflection on the ObjectProperties
        # class for determining this as it is possible to compute intensities based on transformations
        # of the properties objects (i.e. don't tie what can be computed with what is provided too closely)
        prop = props.cell if self.component == 'cell' else props.nucleus
        values = _quantify_intensities(signals[z], prop)
        if len(values) != self.n_channels:
            raise AssertionError(
                'Expecting {} {} intensity measurements but got result {}'
                .format(self.n_channels, self.component, values)
            )
        return values


class GraphFeatures(FeatureCalculator):

    def __init__(self, prefix):
        self.prefix = prefix

    def get_feature_names(self, ):
        feature_names = ['n_neighbors', 'neighbors', 'adj_neighbor_pct', 'adj_bg_pct']
        return [self.prefix + c for c in feature_names]

    def get_feature_values(self, signals, labels, graph, props, z):
        # graph.adj behaves like a dict keyed by node id where each node id is an integer label in the
        # labeled image and the value associated is another dictionary keyed by neighbor node ids (with
        # values equal to the data associated with the edge).  Examples:
        # rag.adj[1] --> AtlasView({2: {'weight': 1.0, 'count': 24}})
        # rag.adj[1][2] --> {'weight': 1.0, 'count': 24}
        # Also note that if a background class is present all nodes will be neighbors of it, but if there is no
        # background (when watershed returns no 0 labeled images if no mask given) then there will be no "0"
        # node id (so be careful with assuming its there)

        # Get the edges/neighbors data from the graph for this cell
        nbrs = graph.adj[props.cell.label]

        # Get list of non-bg neighbor ids
        nids = [nid for nid in nbrs.keys() if nid != 0]

        # Get raw weight (which is number of bordering pixels on both sides of boundary)
        # associated with each non-bg neighbor
        nbwts = np.array([nbrs[nid]['count'] for nid in nids])

        # Get raw weight of background, if present
        bgwt = nbrs[0]['count'] if 0 in nbrs else 0

        wtsum = bgwt + nbwts.sum()
        assert wtsum > 0, \
            'Cell {} has no neighbors and associated boundary pixel counts (this should not be possible)'\
            .format(props.cell.label)
        return [
            len(nids),
            ','.join([str(nid) for nid in nids]),
            list(nbwts / wtsum),
            bgwt / wtsum
        ]


class Cytometer2D(KerasCytometer2D):

    def _get_model(self, input_shape):
        # Load this as late as possible to avoid premature keras backend initialization
        from codex.cytometry.models import unet_v2 as unet_model
        return unet_model.get_model(3, input_shape)

    def _get_weights_path(self):
        # Load this as late as possible to avoid premature keras backend initialization
        from codex.cytometry.models import unet_v2 as unet_model
        path = osp.join(codex_data.get_cache_dir(), 'cytometry', 'unet_v2_weights.h5')
        return codex_data.download_file_from_google_drive(unet_model.WEIGHTS_FILE_ID, path, name='UNet Weights')

    def get_segmentation_mask(self, img_bin_nuci, img_memb=None, dilation_factor=0, sigma=None, gamma=None):
        if dilation_factor > 0:
            img_bin_nuci = cv2.dilate(
                img_bin_nuci.astype(np.uint8),
                morphology.disk(dilation_factor)
            ).astype(np.bool)
        if img_memb is None:
            return img_bin_nuci

        # Construct mask as threshold on membrane image OR binary nucleus mask
        if sigma is not None:
            img_memb = filters.gaussian(img_memb, sigma=sigma)
        if gamma is not None:
            img_memb = exposure.adjust_gamma(img_memb, gamma=gamma)
        img_bin_memb = img_memb > filters.threshold_otsu(img_memb)
        img_bin_memb = img_bin_memb | img_bin_nuci
        return img_bin_memb

    def segment(self, img_nuc, img_memb=None, nucleus_dilation=4, min_size=12,
                membrane_sigma=None, membrane_gamma=None,
                batch_size=DEFAULT_BATCH_SIZE, return_masks=False):
        if not self.initialized:
            self.initialize()

        # Convert images to segment or otherwise analyze to 8-bit
        img_nuc = _to_uint8(img_nuc, 'nucleus')
        if img_memb is not None:
            img_memb = _to_uint8(img_memb, 'membrane')

        # Add z dimension (equivalent to batch dim in this case) if not present
        if img_nuc.ndim == 2:
            img_nuc = np.expand_dims(img_nuc, 0)
        if img_nuc.ndim != 3:
            raise ValueError('Must provide image as ZHW or HW (image shape given = {})'.format(img_nuc.shape))

        # Make predictions on image converted to 0-1 and with trailing channel dimension to give NHWC;
        # Result has shape NHWC where C=3 and C1 = bg, C2 = interior, C3 = border
        img_pred = self.predict(np.expand_dims(img_nuc / 255., -1), batch_size)
        assert img_pred.shape[-1] == 3, \
            'Expecting 3 outputs in predictions (shape = {})'.format(img_pred.shape)

        img_seg_list, img_bin_list = [], []
        nz = img_nuc.shape[0]
        for i in range(nz):

            # Use nuclei interior mask as watershed markers
            img_bin_nucm = np.argmax(img_pred[i], axis=-1) == 1

            # Remove markers (which determine number of cells) below the given size
            if min_size > 0:
                img_bin_nucm = morphology.remove_small_objects(img_bin_nucm, min_size=min_size)

            # Define the entire nucleus interior as a slight dilation of the markers noting that this
            # actually works better than using the union of predicted interiors and predicted boundaries
            # (which are often too thick)
            img_bin_nuci = cv2.dilate(img_bin_nucm.astype(np.uint8), morphology.disk(1)).astype(np.bool)

            # Label the markers and create the basin to segment over
            img_bin_nucm_label = morphology.label(img_bin_nucm)
            img_basin = -1 * ndimage.distance_transform_edt(img_bin_nucm)

            # Determine the overall mask to segment across by dilating nuclei by some fixed amount
            # or if possible, using the given cell membrane image
            img_bin_mask = self.get_segmentation_mask(
                img_bin_nuci, img_memb=img_memb[i] if img_memb is not None else None,
                dilation_factor=nucleus_dilation, sigma=membrane_sigma, gamma=membrane_gamma)

            # Run watershed using markers and expanded nuclei / cell mask
            img_cell_seg = segmentation.watershed(img_basin, img_bin_nucm_label, mask=img_bin_mask)

            # Generate nucleus segmentation based on cell segmentation and nucleus mask
            # and relabel nuclei objections using corresponding cell labels
            img_nuc_seg = (img_cell_seg > 0) & img_bin_nuci
            img_nuc_seg = img_nuc_seg * img_cell_seg

            # Add labeled images to results
            assert img_cell_seg.dtype == img_nuc_seg.dtype, \
                'Cell segmentation dtype {} != nucleus segmentation dtype {}'\
                .format(img_cell_seg.dtype, img_nuc_seg.dtype)
            img_seg_list.append(np.stack([img_cell_seg, img_nuc_seg], axis=0))

            # Add mask images to results, if requested
            if return_masks:
                img_bin_list.append(np.stack([img_bin_nuci, img_bin_nucm, img_bin_mask], axis=0))

        assert nz == len(img_seg_list)
        if return_masks:
            assert nz == len(img_bin_list)

        # Stack final segmentation image as (z, c, h, w)
        img_seg = np.stack(img_seg_list, axis=0)
        img_bin = np.stack(img_bin_list, axis=0) if return_masks else None
        assert img_seg.ndim == 4, 'Expecting 4D segmentation image but shape is {}'.format(img_seg.shape)

        # Return (in this order) labeled volumes, prediction volumes, mask volumes
        return img_seg, img_pred, img_bin

    def quantify(self, tile, img_seg, channel_names=None,
                 cell_intensity_prefix=DEFAULT_CELL_INTENSITY_PREFIX,
                 nucleus_intensity_prefix=DEFAULT_NUCL_INTENSITY_PREFIX,
                 cell_graph_prefix=DEFAULT_CELL_GRAPH_PREFIX,
                 include_cell_intensity=True,
                 include_nucleus_intensity=False,
                 include_cell_graph=False):
        ncyc, nz, _, nh, nw = tile.shape

        # Move cycles and channels to last axes (in that order)
        tile = np.moveaxis(tile, 0, -1)
        tile = np.moveaxis(tile, 1, -1)

        # Collapse tile to ZHWC (instead of cycles and channels being separate)
        tile = np.reshape(tile, (nz, nh, nw, -1))
        nch = tile.shape[-1]

        if channel_names is not None and nch != len(channel_names):
            raise ValueError(
                'Tile has {} channels but given channel name list has {} (they should be equal); '
                'channel names given = {}, tile shape = {}'
                .format(nch, len(channel_names), channel_names, tile.shape)
            )

        # Configure features to be calculated based on provided flags
        feature_calculators = [BasicCellFeatures()]
        if include_cell_intensity:
            feature_calculators.append(IntensityFeatures(nch, cell_intensity_prefix, 'cell', channel_names))
        if include_nucleus_intensity:
            feature_calculators.append(IntensityFeatures(nch, nucleus_intensity_prefix, 'nucleus', channel_names))
        if include_cell_graph:
            feature_calculators.append(GraphFeatures(cell_graph_prefix))

        # Compute list of resulting feature names (values will be added in this order)
        feature_names = [v for fc in feature_calculators for v in fc.get_feature_names()]

        feature_values = []
        for z in range(nz):
            # Calculate properties of masked+labeled cell components
            cell_props = measure.regionprops(img_seg[z][CELL_CHANNEL], cache=False)
            nucleus_props = measure.regionprops(img_seg[z][NUCLEUS_CHANNEL], cache=False)
            if len(cell_props) != len(nucleus_props):
                raise ValueError(
                    'Expecting cell and nucleus properties to have same length (nucleus props = {}, cell props = {})'
                    .format(len(nucleus_props), len(cell_props))
                )

            # Compute RAG for cells if necessary
            graph = None
            if include_cell_graph:
                labels = img_seg[z][CELL_CHANNEL]

                # rag_boundary fails on all zero label matrices so default to empty graph if that is the case
                # see: https://github.com/scikit-image/scikit-image/blob/master/skimage/future/graph/rag.py#L386
                if np.count_nonzero(labels) > 0:
                    graph = label_graph.rag_boundary(labels, np.ones(labels.shape))
                else:
                    graph = label_graph.RAG()

            # Loop through each detected cell and compute features
            for i in range(len(cell_props)):
                props = ObjectProperties(cell=cell_props[i], nucleus=nucleus_props[i])

                # Run each feature calculator and add results in order
                feature_values.append([
                    v for fc in feature_calculators
                    for v in fc.get_feature_values(tile, img_seg, graph, props, z)
                ])

        return pd.DataFrame(feature_values, columns=feature_names)


# def _get_flat_ball(size):
#     struct = morphology.ball(size)
#
#     # Ball structs should always be of odd size and double given radius pluse one
#     assert struct.shape[0] == size * 2 + 1
#     assert struct.shape[0] % 2 == 1
#
#     # Get middle index (i.e. position 2 (0-index 1) for struct of size 3)
#     mid = ((struct.shape[0] + 1) // 2) - 1
#
#     # Flatten the ball so there is no connectivity in the z-direction
#     struct[(mid + 1):] = 0
#     struct[:(mid)] = 0
#
#     return struct

# class Cytometer3D(Cytometer):
#
#     def _get_model(self, input_shape):
#         return _get_unet_v1_model(input_shape)
#
#     def prepocess(self, img, thresh, min_size):
#         img = img > thresh
#         if min_size > 0:
#             # img = morphology.remove_small_holes(img, area_threshold=min_size)
#             img = np.stack([morphology.remove_small_objects(img[i], min_size=min_size) for i in range(img.shape[0])])
#         return img
#
#     def get_segmentation_mask(self, img_bin_nuci, dilation_factor=0):
#         if dilation_factor > 0:
#             return morphology.dilation(img_bin_nuci, selem=_get_flat_ball(dilation_factor))
#         else:
#             return img_bin_nuci
#
#     def segment(self, img, nucleus_dilation=4, proba_threshold=.5, min_size=6, batch_size=DEFAULT_BATCH_SIZE):
#         if not self.initialized:
#             self.initialize()
#
#         if img.dtype != np.uint8:
#             raise ValueError('Must provide uint8 image not {}'.format(img.dtype))
#         if img.squeeze().ndim != 3:
#             raise ValueError(
#                 'Must provide single, 3D grayscale (or an image with other unit dimensions) '
#                 'image but not image with shape {}'.format(img.shape))
#         img = img.squeeze()
#         nz = img.shape[0]
#
#         img_pred = self.model.predict(np.expand_dims(img, -1) / 255., batch_size=batch_size)
#         assert img_pred.shape[0] == nz, \
#             'Expecting {} predictions but got result with shape {}'.format(nz, img_pred.shape)
#
#         # Extract prediction channels
#         img_bin_nuci, img_bin_nucb, img_bin_nucm = [self.prepocess(img_pred[..., i], proba_threshold, min_size) for i in range(3)]
#
#         # Form watershed markers as marker class intersection with nuclei class, minus boundaries
#         img_bin_nucm = img_bin_nucm & img_bin_nuci & ~img_bin_nucb
#
#         # Label the markers and create the basin to segment (+boundary, -nucleus interior)
#         img_bin_nucm_label = morphology.label(img_bin_nucm)
#         img_bin_nuci_basin = ndimage.distance_transform_edt(img_bin_nuci)
#         img_bin_nucb_basin = ndimage.distance_transform_edt(img_bin_nucb)
#         img_basin = -img_bin_nuci_basin + img_bin_nucb_basin
#
#         # Determine the overall mask to segment across by dilating nuclei as an approximation for cytoplasm/membrane
#         seg_mask = self.get_segmentation_mask(img_bin_nuci, dilation_factor=nucleus_dilation)
#
#         # Run segmentation and return results
#         img_seg = segmentation.watershed(img_basin, img_bin_nucm_label, mask=seg_mask)
#
#         return img_seg, img_pred, np.stack([img_bin_nuci, img_bin_nucb, img_bin_nucm], axis=-1)
#
#     def quantify(self, tile, cell_segmentation, channel_names=None, channel_name_prefix='ch:'):
#         ncyc, nz, _, nh, nw = tile.shape
#
#         # Move cycles and channels to last axes (in that order)
#         tile = np.moveaxis(tile, 0, -1)
#         tile = np.moveaxis(tile, 1, -1)
#
#         # Collapse tile to ZHWC (instead of cycles and channels being separate)
#         tile = np.reshape(tile, (nz, nh, nw, -1))
#         nch = tile.shape[-1]
#
#         if channel_names is None:
#             channel_names = ['{}{:03d}'.format(channel_name_prefix, i) for i in range(nch)]
#         else:
#             channel_names = [channel_name_prefix + c for c in channel_names]
#         if nch != len(channel_names):
#             raise ValueError(
#                 'Data tile contains {} channels but channel names list contains only {} items '
#                 '(names given = {}, tile shape = {})'
#                     .format(nch, len(channel_names), channel_names, tile.shape))
#
#         res = []
#         props = measure.regionprops(cell_segmentation)
#         for i, prop in enumerate(props):
#             # Get a (n_pixels, n_channels) array of intensity values associated with
#             # this region and then average across n_pixels dimension
#             intensities = tile[prop.coords[:, 0], prop.coords[:, 1], prop.coords[:, 0]].mean(axis=0)
#             assert intensities.ndim == 1
#             assert len(intensities) == nch
#             row = [prop.label, prop.centroid[2], prop.centroid[1], prop.centroid[0], prop.area, prop.solidity]
#             row += list(intensities)
#             res.append(row)
#
#         return pd.DataFrame(res, columns=['id', 'x', 'y', 'z', 'volume', 'solidity'] + channel_names)