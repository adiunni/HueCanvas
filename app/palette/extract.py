from io import BytesIO
import colorsys
import warnings
import numpy as np
from PIL import Image, ImageOps
from sklearn.cluster import KMeans

def extract_palette(data: bytes, count=4):
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(BytesIO(data)) as source:
            if source.format not in ('JPEG', 'PNG'):
                raise ValueError('Use JPEG or PNG artwork')
            image = ImageOps.exif_transpose(source).convert('RGB')
            image.thumbnail((120, 120))
            pixels = np.asarray(image).reshape(-1, 3)
    hsv = np.array([colorsys.rgb_to_hsv(*(p/255)) for p in pixels])
    useful = (hsv[:,1] > .18) & (hsv[:,2] > .06) & (hsv[:,2] < .98)
    if useful.sum() > len(pixels)*.08:
        pixels = pixels[useful]
    k = min(count, len(np.unique(pixels, axis=0)))
    model = KMeans(n_clusters=k, random_state=42, n_init=5).fit(pixels)
    order = np.argsort(np.bincount(model.labels_))[::-1]
    colors = ['#' + ''.join(f'{int(v):02x}' for v in np.clip(np.rint(model.cluster_centers_[i]),0,255)) for i in order]
    return (colors * count)[:count]
