"""Read macOS audio routing; change output only on an explicit UI action."""
import ctypes as C
import sys


def fourcc(value):
    return int.from_bytes(value.encode('ascii'), 'big')


class Address(C.Structure):
    _fields_ = [('selector', C.c_uint32), ('scope', C.c_uint32), ('element', C.c_uint32)]


class CoreAudio:
    def __init__(self):
        self.audio=C.CDLL('/System/Library/Frameworks/CoreAudio.framework/CoreAudio')
        self.cf=C.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        for name,args,result in [
            ('AudioObjectGetPropertyDataSize',[C.c_uint32,C.POINTER(Address),C.c_uint32,C.c_void_p,C.POINTER(C.c_uint32)],C.c_int32),
            ('AudioObjectGetPropertyData',[C.c_uint32,C.POINTER(Address),C.c_uint32,C.c_void_p,C.POINTER(C.c_uint32),C.c_void_p],C.c_int32),
            ('AudioObjectSetPropertyData',[C.c_uint32,C.POINTER(Address),C.c_uint32,C.c_void_p,C.c_uint32,C.c_void_p],C.c_int32),
            ('AudioObjectHasProperty',[C.c_uint32,C.POINTER(Address)],C.c_bool),
        ]:
            function=getattr(self.audio,name);function.argtypes=args;function.restype=result
        self.cf.CFStringGetCString.argtypes=[C.c_void_p,C.c_void_p,C.c_long,C.c_uint32]
        self.cf.CFStringGetCString.restype=C.c_bool
        self.cf.CFRelease.argtypes=[C.c_void_p];self.cf.CFRelease.restype=None

    def address(self,selector,scope='glob'):
        return Address(fourcc(selector),fourcc(scope),0)

    def values(self,object_id,selector,scope='glob'):
        address=self.address(selector,scope);size=C.c_uint32()
        if not self.audio.AudioObjectHasProperty(object_id,C.byref(address)):return []
        if self.audio.AudioObjectGetPropertyDataSize(object_id,C.byref(address),0,None,C.byref(size)):
            raise ValueError('Cannot inspect the Mac audio devices')
        values=(C.c_uint32*(size.value//4))()
        if self.audio.AudioObjectGetPropertyData(object_id,C.byref(address),0,None,C.byref(size),values):
            raise ValueError('Cannot inspect the Mac audio devices')
        return list(values)

    def string(self,object_id,selector):
        address=self.address(selector);value=C.c_void_p();size=C.c_uint32(C.sizeof(value))
        if self.audio.AudioObjectGetPropertyData(object_id,C.byref(address),0,None,C.byref(size),C.byref(value)):
            return ''
        if not value:return ''
        try:
            result=C.create_string_buffer(4096)
            return result.value.decode('utf-8') if self.cf.CFStringGetCString(value,result,len(result),0x08000100) else ''
        finally:self.cf.CFRelease(value)

    def devices(self):
        result=[]
        for id in self.values(1,'dev#'):
            members=[self.string(member,'uid ') for member in self.values(id,'agrp')]
            result.append({'id':id,'name':self.string(id,'lnam'),'uid':self.string(id,'uid '),'members':members,
                           'output':bool(self.values(id,'stm#','outp'))})
        return result

    def default_output(self):
        values=self.values(1,'dOut')
        return values[0] if values else None

    def set_output(self,id):
        address=self.address('dOut');value=C.c_uint32(id)
        if self.audio.AudioObjectSetPropertyData(1,C.byref(address),0,None,C.sizeof(value),C.byref(value)):
            raise ValueError('Cannot change Mac sound output. Select the Multi-Output Device in System Settings → Sound.')


def describe_route(devices,default_id,input_name='BlackHole 2ch'):
    source=next((d for d in devices if d['name']==input_name),None)
    output=next((d for d in devices if d['id']==default_id),None)
    uid=source['uid'] if source else None
    routed=bool(uid and output and (output['uid']==uid or uid in output['members']))
    audible={d['uid'] for d in devices if d.get('output') and d['uid']!=uid and 'blackhole' not in d['name'].lower()}
    candidates=[{'id':d['id'],'name':d['name']} for d in devices if uid and uid in d['members'] and audible.intersection(d['members'])]
    candidates.sort(key=lambda d:('multi-output' not in d['name'].lower(),d['name']))
    return {'available':True,'output':output['name'] if output else None,'routed':routed,
            'input':input_name,'candidates':candidates,
            'message':None if routed else f'Mac output is {output["name"] if output else "unavailable"}. Select a Multi-Output Device containing {input_name} and your speakers/headphones.'}


def routing_status(input_name='BlackHole 2ch'):
    if sys.platform!='darwin':return {'available':False,'routed':None,'candidates':[]}
    try:
        core=CoreAudio()
        return describe_route(core.devices(),core.default_output(),input_name)
    except (OSError,ValueError) as exc:
        return {'available':False,'routed':None,'candidates':[],'message':str(exc)}


def set_music_output(id,input_name='BlackHole 2ch'):
    if sys.platform!='darwin':raise ValueError('Output routing is available on macOS only')
    core=CoreAudio();route=describe_route(core.devices(),core.default_output(),input_name)
    if id not in {candidate['id'] for candidate in route['candidates']}:
        raise ValueError('Choose a Multi-Output Device containing BlackHole and a second output. Refresh routing after changing Audio MIDI Setup.')
    core.set_output(id)
    return routing_status(input_name)
