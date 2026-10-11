def hex_to_xy(color):
    rgb = [int(color[i:i+2], 16) / 255 for i in (1, 3, 5)]
    r, g, b = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in rgb]
    x = .664511*r + .154324*g + .162028*b
    y = .283881*r + .668433*g + .047685*b
    z = .000088*r + .072310*g + .986039*b
    total = x + y + z
    return [x/total, y/total] if total else [.3127, .3290]
