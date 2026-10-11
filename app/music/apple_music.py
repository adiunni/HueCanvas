import asyncio
import sys
class AppleMusic:
    async def current_track(self):
        if sys.platform != 'darwin':
            return {'available':False,'reason':'macOS Music integration is only available on macOS'}
        script = '''tell application "System Events" to set musicRunning to exists process "Music"
if not musicRunning then return "unavailable"
tell application "Music"
if player state is stopped then return "unavailable"
set t to current track
return (name of t) & linefeed & (artist of t) & linefeed & (album of t)
end tell'''
        process = await asyncio.create_subprocess_exec('osascript','-e',script,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(process.communicate(),timeout=8)
        except asyncio.TimeoutError:
            process.kill(); await process.communicate()
            return {'available':False,'reason':'Music scripting timed out'}
        if process.returncode or out.strip() == b'unavailable':
            return {'available':False,'reason':err.decode().strip() or 'Music is not playing'}
        parts = out.decode().strip().splitlines()
        if len(parts) < 3: return {'available':False,'reason':'Incomplete track metadata'}
        return {'available':True,'track':parts[0],'artist':parts[1],'album':parts[2]}
    async def artwork(self, path):
        # Music's scripting dictionary exposes raw artwork data.
        script = '''on run argv
tell application "Music"
set artworkData to raw data of artwork 1 of current track
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
        process = await asyncio.create_subprocess_exec('osascript','-e',script,str(path),stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        try:
            _, err = await asyncio.wait_for(process.communicate(),8)
        except asyncio.TimeoutError:
            process.kill(); await process.communicate(); raise ValueError('Artwork scripting timed out')
        if process.returncode: raise ValueError(err.decode().strip() or 'Artwork unavailable')
