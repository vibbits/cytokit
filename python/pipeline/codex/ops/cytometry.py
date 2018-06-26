from codex.ops import op as codex_op
from codex.cytometry import cytometer
from codex import io as codex_io
import os
import os.path as osp
import codex
import logging
import numpy as np
logger = logging.getLogger(__name__)


def get_model_path():
    return os.getenv(
        codex.ENV_CYTOMETRY_2D_MODEL_PATH,
        osp.join(os.environ['CODEX_DATA_DIR'], 'modeling', 'cytopy', 'models', 'nuclei', 'v0.3', 'nuclei_model.h5')
    )


def set_keras_session(op):
    import keras.backend.tensorflow_backend as KTF
    import tensorflow as tf
    tf_config = codex_op.get_tf_config(op)
    KTF.set_session(tf.Session(config=tf_config))


def close_keras_session():
    import keras.backend.tensorflow_backend as KTF
    KTF.get_session().close()


class Cytometry(codex_op.CodexOp):

    def __init__(self, config, mode='2D', segmentation_params=None, quantification_params=None):
        super(Cytometry, self).__init__(config)

        params = config.cytometry_params
        self.mode = params.get('mode', mode)
        self.segmentation_params = params.get('segmentation_params', segmentation_params or {})

        self.quantification_params = params.get('quantification_params', quantification_params or {})
        if 'channel_names' not in self.quantification_params:
            self.quantification_params['channel_names'] = self.config.channel_names

        self.nuc_channel_coords = config.get_channel_coordinates(params['nuclei_channel_name'])
        self.mem_channel_coords = None if 'membrane_channel_name' not in params else \
            config.get_channel_coordinates(params['membrane_channel_name'])

        if self.mode != '2D':
            raise ValueError('Cytometry mode should be one of ["2D"] not {}'.format(self.mode))
        self.input_shape = (config.tile_height, config.tile_width, 1)
        self.cytometer = None

    def initialize(self):
        # Set the Keras session to have the same TF configuration as other operations
        set_keras_session(self)

        # Load the cytometry model from path to keras model weights
        model_path = get_model_path()
        logger.debug('Initializing cytometry model from path "{}" (input shape = {})'.format(model_path, self.input_shape))
        self.cytometer = cytometer.Cytometer2D(self.input_shape, model_path).initialize()
        return self

    def shutdown(self):
        close_keras_session()
        return self

    def _run_2d(self, tile):
        # Tile should have shape (cycles, z, channel, height, width)
        nuc_cycle = self.nuc_channel_coords[0]
        nuc_channel = self.nuc_channel_coords[1]
        img_nuc = tile[nuc_cycle, :, nuc_channel]

        if self.mem_channel_coords is not None:
            memb_cycle = self.nuc_channel_coords[0]
            memb_channel = self.nuc_channel_coords[1]
            img_memb = tile[memb_cycle, :, memb_channel]

        img_seg, img_pred, img_bin = self.cytometer.segment(img_nuc, img_memb=img_memb, **self.segmentation_params)

        # Ensure segmentation image is of integer type and >= 0
        assert np.issubdtype(img_seg.dtype, np.integer), \
            'Expecting int segmentation image but got {}'.format(img_seg.dtype)
        assert img_seg.min() >= 0, \
            'Labeled segmentation image contains label < 0 (shape = {}, dtype = {})'\
            .format(img_seg.shape, img_seg.dtype)

        # Check to make sure we did not end up with more than the maximum possible number of labeled cells
        if img_seg.max() > np.iinfo(np.uint16).max:
            raise ValueError(
                'Segmentation resulted in {} cells, a number which is both suspiciously high '
                'and too large to store as the assumed 16-bit format'.format(img_seg.max()))

        stats = self.cytometer.quantify(tile, img_seg, **self.quantification_params)

        # Create overlay image of nucleus channel and boundaries and convert to 5D
        # shape to conform with usual tile convention

        img_boundary = np.stack([
            _find_boundaries(img_seg[:, i], as_binary=False)
            for i in range(img_seg.shape[1])
        ], axis=1)
        assert img_boundary.ndim == 4, 'Expecting 4D image, got shape {}'.format(img_boundary.shape)

        # Stack labeled volumes to 5D tiles and convert to uint16
        img_label = np.stack([img_seg, img_boundary], axis=0).astype(np.uint16)

        # Add cycle axis to mask volumes to give 5D tile as uint8
        img_bin = (img_bin[np.newaxis] * np.iinfo(np.uint8).max).astype(np.uint8)

        return img_label, img_bin, stats

    def _run(self, tile, **kwargs):
        return self._run_2d(tile)

    def save(self, tile_indices, output_dir, data):
        region_index, tile_index, tx, ty = tile_indices
        img_label, img_bin, stats = data

        label_tile_path = codex_io.get_cytometry_segmentation_path(region_index, tx, ty)
        codex_io.save_tile(osp.join(output_dir, label_tile_path), img_label)

        mask_tile_path = codex_io.get_cytometry_mask_path(region_index, tx, ty)
        codex_io.save_tile(osp.join(output_dir, mask_tile_path), img_bin)

        # Append useful metadata to cytometry stats (align these names to those used in config.TileDims)
        stats.insert(0, 'tile_y', ty)
        stats.insert(0, 'tile_x', tx)
        stats.insert(0, 'tile_index', tile_index)
        stats.insert(0, 'region_index', region_index)
        stats_path = codex_io.get_cytometry_stats_path(region_index, tx, ty)
        stats.to_csv(osp.join(output_dir, stats_path), index=False)

        return label_tile_path, mask_tile_path, stats_path


def _find_boundaries(img, as_binary=False):
    """Identify boundaries in labeled image volume

    Args:
        img: A labeled 3D volume with shape (z, h, w)
        as_binary: Flag indicating whether to return binary boundary image or labeled boundaries
    """
    from skimage import segmentation
    assert img.ndim == 3, 'Expecting 3D volume but got image with shape {}'.format(img.shape)

    # Find boundaries (per z-plane since find_boundaries is buggy in 3D)
    res = np.stack([
        segmentation.find_boundaries(img[i], mode='inner', background=img.min())
        for i in range(img.shape[0])
    ], axis=0)

    return res if as_binary else res * img

