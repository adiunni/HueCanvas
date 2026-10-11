from app.hue.models import Light
class MockClient:
    def __init__(self):
        self.lights = [Light(id=str(i), name=f'{"Color" if i<2 else "White"} {"left" if i%2==0 else "right"}', color=i<2, dimming=True, enabled=True, role=['COLOR_LEFT','COLOR_RIGHT','WHITE_LEFT','WHITE_RIGHT'][i]) for i in range(4)]
        self.commands = []
    async def discover_lights(self):
        return [x.model_copy(deep=True) for x in self.lights]
    async def update(self, light_id, payload):
        light = next(x for x in self.lights if x.id == light_id)
        if 'color' in payload and not light.color:
            raise ValueError('Unsupported color command')
        self.commands.append((light_id, payload))
        self.commands = self.commands[-200:]
        if 'on' in payload: light.on = payload['on']['on']
        if 'dimming' in payload: light.brightness = payload['dimming']['brightness']
        if 'color' in payload: light.xy = list(payload['color']['xy'].values())
    async def close(self): pass
