from app.palette.convert import hex_to_xy
from app.palette.gamut import map_gamut

def command(light, brightness, color=None, duration=3, on=True):
    payload = {'on': {'on': on}, 'dynamics': {'duration': int(max(1,duration)*1000)}}
    if light.dimming:
        payload['dimming'] = {'brightness': max(1,min(100,brightness))}
    if light.color and color:
        xy = map_gamut(hex_to_xy(color), light.gamut)
        payload['color'] = {'xy': {'x': xy[0], 'y': xy[1]}}
    return payload
