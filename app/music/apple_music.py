import asyncio
import sys


async def run_script(script,*args,timeout=3):
    process=await asyncio.create_subprocess_exec('osascript','-e',script,*map(str,args),
        stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    try:
        out,err=await asyncio.wait_for(process.communicate(),timeout)
    except (asyncio.TimeoutError,asyncio.CancelledError):
        if process.returncode is None:process.kill()
        await process.communicate()
        raise
    if process.returncode:raise ValueError(err.decode().strip() or 'Music scripting failed')
    return out.decode().rstrip('\n')


class AppleMusic:
    async def current_track(self):
        if sys.platform!='darwin':return {'available':False,'reason':'macOS Music integration is only available on macOS'}
        script='''tell application "System Events" to set musicRunning to exists process "Music"
if not musicRunning then return "unavailable"
tell application "Music"
if player state is stopped then return "unavailable"
set t to current track
set sep to ASCII character 31
return (persistent ID of t) & sep & (name of t) & sep & (artist of t) & sep & (album of t) & sep & (player state as text) & sep & (player position as text) & sep & (duration of t as text)
end tell'''
        try:out=await run_script(script,timeout=3)
        except asyncio.TimeoutError:return {'available':False,'reason':'Music scripting timed out'}
        except ValueError as exc:return {'available':False,'reason':str(exc)}
        if out=='unavailable':return {'available':False,'reason':'Music is not playing'}
        parts=out.split('\x1f')
        if len(parts) not in (5,7):return {'available':False,'reason':'Incomplete track metadata'}
        track={'available':True,'id':parts[0],'track':parts[1],'artist':parts[2],'album':parts[3],'playing':parts[4]=='playing'}
        if len(parts)==7:
            try:track.update(position=max(0,float(parts[5].replace(',','.'))),duration=float(parts[6].replace(',','.')),rate=1.)
            except ValueError:pass
        return track

    async def artwork(self,path,track_id=''):
        script='''on run argv
tell application "Music"
if (item 2 of argv) is not "" and (persistent ID of current track) is not (item 2 of argv) then error "Track changed before artwork was read"
set artworkData to raw data of artwork 1 of current track
if (item 2 of argv) is not "" and (persistent ID of current track) is not (item 2 of argv) then error "Track changed while artwork was read"
end tell
set f to open for access POSIX file (item 1 of argv) with write permission
try
set eof f to 0
write artworkData to f
close access f
on error messageText
close access f
error messageText
end try
end run'''
        try:await run_script(script,path,track_id,timeout=4)
        except asyncio.TimeoutError as exc:raise ValueError('Artwork scripting timed out') from exc
