from tqdm import tqdm
from collections import OrderedDict
import glob
import warnings
import copy
import os
import logging
import time
import random
import numpy as np
import scipy
import scipy.misc
from scipy.cluster import vq
from scipy import interpolate
from PIL import Image
from PIL import ImageFilter
import sqlite3
import color_spaces as cs
from directory_walker import DirectoryWalker
from memo import memo
from progress_bar import progress_bar
from skimage import draw, img_as_float
from skimage.io import imread
from skimage.transform import resize
import colorspacious


def simple(image, pool):
    """
    Complete example

    Parameters
    ----------
    image : array
    pool : dict-like
        output from :func:`make_pool`; or any mapping of
        arguments for opening an image file to a vector characterizing it:
        e.g., ``{(filename,): [1, 2, 3]}``

    Returns
    -------
    mosaic : array
    """
    partition(image, grid_size=(10, 10), depth=1)
    matcher = SimpleMatcher(pool)
    tile_colors = [analyzer(image[tile]) for tile in tiles]
    matches = []
    for tile_color in tqdm(tile_colors, total=len(tile_colors)):
        matches.append(matcher.match(tile_color))
    canvas = np.ones_likes(image)  # white canvas same shape as input image
    return draw_mosaic(canvas, tiles, matches)


def make_pool(glob_string, *, cache=None, skip_read_failures=True,
              analyzer=dominant_color):
    """
    Analyze a collection of images.

    For each file:
    1. Read image.
    2. Convert to perceptually-uniform color space "CIECAM02".
    3. Characterize the colors in the image as a vector.

    A progress bar is displayed and then hidden after completion.

    Parameters
    ----------
    glob_string : string
        a filepath with optional wildcards, like `'*.jpg'`
    pool : dict-like, optional
        dict-like data structure to hold results; if None, dict is used
    skip_read_failures: bool, optional
        If True (default), convert any exceptions that occur while reading a
        file into warnings and continue.
    analyzer : callable, optional
        Function with signature: ``f(img) -> arr`` where ``arr`` is a vector.
        The default analyzer is :func:`dominant_color`.

    Returns
    -------
    cache : dict-like
        mapping arguments for opening file to analyzer's result, e.g.:
        ``{(filename,): [1, 2, 3]}``
    """
    if pool is None:
        pool = {}
    filenames = glob.glob(glob_string)
    for filename in tqdm(filenames):
        try:
            raw_image = imread(filename)
            image = img_as_float(raw_image)  # ensure float scaled 0-1
        except Exception as err:
            if skip_read_failures:
                warnings.warn("Skipping {}; raised exception:\n    {}"
                             "".format(filename, err))
                continue
            raise
        # Assume last axis is color axis. If alpha channel exists, drop it.
        if image.shape[-1] == 4:
            image = image[:, :, :-1]
        # Convert color to perceptually-uniform color space.
        # "JCh" is a simplified "CIECAM02".
        percep = colorspacious.cspace_convert(image, "sRGB1", "JCh")
        vector = analyzer(percep)
        pool[(filename,)] = vector
    return pool 


def dominant_color(image, n_clusters=5, sample_size=1000):
    """
    Sample pixels from an image, cluster colors, and identify dominant color.

    Parameters
    ----------
    image: array
        The last axis is expected to be the color axis.
    n_clusters : int, optional
        number of clusters; default 5
    sample_size : int, optional
        number of pixels to sample; default 1000

    Returns
    -------
    dominant_color : array
    """
    image = copy.deepcopy(image)
    # 'raveled_image' is a 2D array, a 1D list of 1D color vectors
    raveled_image = image.reshape(scipy.product(image.shape[:-1]),
                                  image.shape[-1])
    np.random.shuffle(raveled_image)  # shuffles in place
    sample = raveled_image[:min(len(raveled_image), sample_size)]
    colors, dist = vq.kmeans(sample, n_clusters)
    vecs, dist = vq.vq(sample, colors)
    counts, bins = scipy.histogram(vecs, len(colors))
    return colors[counts.argmax()]


class SimpleMatcher:
    """
    This simple matcher returns the closest match.

    It maintains an internal tree representation of the pool for fast lookups.
    """
    def __init__(self, pool):
        self._pool = OrderedDict(pool)
        self._args = list(self._pool.keys())
        data = np.array([vector for vector in self._pool.values()])
        self._tree = cKDTree(data)

    def match(self, vector):
        distance, index = self._tree.query(vector, k=1)
        return self._pool[index]


def draw_mosaic(image, tiles, matches):
    """
    Assemble the mosaic, the final result.

    Parameters
    ----------
    image : array
        the "canvas" on which to draw the tiles, modified in place
    tiles : list
        list of pairs of slice objects
    tile_matches : list
        for each tile in ``tiles``, a tuple of arguments for opening the
        matching image file

    Returns
    -------
    image : array
    """
    for tile, match_args in zip(tiles, tile_matches):
        raw_match_image = imread(*match_args)
        match_image = resize(raw_match_image, _tile_size(tile))
        image[tile] = match_image
    return image 


def _subdivide(tile):
    "Create four tiles from the four quadrants of the input tile."
    tile_dims = [(s.stop - s.start) // 2 for s in tile]
    subtiles = []
    for y in (0, 1):
        for x in (0, 1):
            subtile = (slice(tile[0].start + y * tile_dims[0],
                             tile[0].start + (1 + y) * tile_dims[0]),
                       slice(tile[1].start + x * tile_dims[1],
                             tile[1].start + (1 + x) * tile_dims[1]))
            subtiles.append(subtile)
    return subtiles


def partition(image, grid_dims, mask=None, depth=0, hdr=80):
    """
    Parition the target image into tiles.

    Optionally, subdivide tiles that contain high contrast, creating a grid
    with tiles of varied size.

    Parameters
    ----------
    grid_dims : int or tuple
        number of (largest) tiles along each dimension
    mask : array, optional
        Tiles that straddle a mask edge will be subdivided, creating a smooth
        edge.
    depth : int, optional
        Default is 0. Maximum times a tile can be subdivided.
    hdr : float
        "High Dynamic Range" --- level of contrast that trigger subdivision

    Returns
    -------
    tiles : list
        list of pairs of slice objects
    """
    # Validate inputs.
    if isinstance(grid_dims, int):
        tile_dims, = image.ndims * (grid_dims,)
    for i in (0, 1):
        image_dim = image.shape[i]
        grid_dim = grid_dims[i]
        if image_dim % grid_dim*2**depth != 0:
            raise ValueError("Image dimensions must be evenly divisible by "
                             "dimensions of the (subdivided) grid. "
                             "Dimension {image_dim} is not "
                             "evenly divisible by {grid_dim}*2**{depth} "
                             "".format(image_dim=image_dim, grid_dim=grid_dim,
                                       depth=depth))

    # Partition into equal-sized tiles (Python slice objects).
    tile_height = image.shape[0] // grid_dims[0] 
    tile_width = image.shape[1] // grid_dims[1]
    tiles = []
    for y in range(grid_dims[0]):
        for x in range(grid_dims[1]):
            tile = (slice(y * tile_height, (1 + y) * tile_height),
                    slice(x * tile_width, (1 + x) * tile_width))
            tiles.append(tile)

    # Discard any tiles that reside fully outside the mask.
    if mask is not None:
        tiles = [tile for tile in tiles if np.any(mask[tile])]

    # If depth > 0, subdivide any tiles that straddle a mask edge or that
    # contain an image with high contrast.
    for _ in range(depth):
        new_tiles = []
        for tile in tiles:
            if (mask is not None) and np.any(mask[tile]) and np.any(~mask[tile]):
                # This tile straddles a mask edge.
                subtiles = _subdivide(tile)
                # Discard subtiles that reside fully outside the mask.
                subtiles = [tile for tile in subtiles if np.any(mask[tile])]
                new_tiles.extend(subtiles)
            elif False:  # TODO hdr check
                new_tiles.extend(_subdivide(tile))
            else:
                new_tiles.append(tile)
        tiles = new_tiles
    return tiles


def _tile_center(tile):
    "Compute (y, x) center of tile."
    return tuple((s.stop + s.start) // 2 for s in tile)


def _tile_size(tile):
    "Compute the (y, x) dimensions of tile."
    return tuple((s.stop - s.start) for s in tile)


def draw_tiles(image, tiles, color=1):
    """
    Draw the tile edges on a copy of image. Make a dot at each tile center.

    This is a utility for inspecting a tile layout.

    Parameters
    ----------
    image : array
    tiles : list
        list of pairs of slices, as generated by :func:`partition`
    color : int or array
        value to "draw" onto ``image`` at tile boundaries

    Returns
    -------
    annotated_image : array
    """
    annotated_image = copy.deepcopy(image)
    for y, x in tiles:
        edges = ((y.start, x.start, y.stop - 1, x.start),
                 (y.stop - 1, x.start, y.stop - 1, x.stop - 1),
                 (y.stop - 1, x.stop - 1, y.start, x.stop - 1),
                 (y.start, x.stop - 1, y.start, x.start))
        for edge in edges:
            rr, cc = draw.line(*edge)
            annotated_image[rr, cc] = color  # tile edges
            annotated_image[_tile_center((y, x))] = color  # dot at tile center
    return annotated_image


def crop_to_fit(image, tile_size):
    "Return a copy of image cropped to precisely fill the dimesions tile_size."
    image_w, image_h = image.shape
    tile_w, tile_h = tile_size
    image_aspect = image_w/image_h
    tile_aspect = tile_w/tile_h
    if image_aspect > tile_aspect:
        # It's too wide.
        crop_h = image_h
        crop_w = int(round(crop_h*tile_aspect))
        x_offset = int((image_w - crop_w)/2)
        y_offset = 0
    else:
        # It's too tall.
        crop_w = image_w
        crop_h = int(round(crop_w/tile_aspect))
        x_offset = 0
        y_offset = int((image_h - crop_h)/2)
    image = image.crop((x_offset,
                    y_offset,
                    x_offset + crop_w,
                    y_offset + crop_h))
    image = image.resize((tile_w, tile_h), Image.ANTIALIAS)
    return image


class Pool:
    "This is probably the wrong approach."
    def __init__(self, load_func, analyze_func, cache_path=None,
                 kdtree_class=None):
        self._load = load_func
        self._analyze = analyze_func
        if kdtree_class is None:
            from scipy.spatial import cKDTree
            kdtree_class = cKDTree
        self._kdtree_class = kdtree_class

        # self._cache maps args for loading image to vector describing it
        if cache_path is None:
            self._cache = {}
        else:
            from historydict import HistoryDict
            self._cache = HistoryDict(cache_path)

        self._keys_in_order = []
        self._data = []
        if self._cache:
            for key, val in self._cache.items():
                self._keys_in_order.append(key)
                self._data.append(val)
        self._build_tree()
        self._stale = False  # stale means KD tree is out-of-date

    def __setstate__(self, d):
        self._load = d['load_func']
        self._analyze = d['analyze_func']
        self._kdtree_class = d['kdtree_class']
        self._cache = d['cache']
        self._keys_in_order = d['keys_in_order']
        self._data = d['data']
        self._stale = d['stale']
        self._tree = d['tree']

    def __getstate__(self):
        d = {}
        d['load_func'] = self._load
        d['analyze_func'] = self._analyze
        d['kdtree_class'] = self._kdtree_class
        d['cache'] = self._cache
        d['keys_in_order'] = self._keys_in_order
        d['data'] = self._data
        d['stale'] = self._stale
        d['tree'] = self._tree
        return d

    def _build_tree(self):
        if not self._data:
            self._tree = self._kdtree_class(np.array([]).reshape(2, 0))
        else:
            self._tree = self._kdtree_class(self._data)

    def add(self, *args):
        arr = self._load_func(*args)
        self._keys_in_order.append(args)
        self._data.append(arr)
        self._cache[args] = self._analyze_func(arr)
        self._stale = True

    def query(self, x, k):
        if self._stale:
            self._build_tree()
            self._stale = False
        return self._tree.query(x, k)
    
    def get_image(self, *args):
        return self._load(*args)
