"""Build on the target OS: python desktop/build_engine.py."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
root=Path(__file__).resolve().parent.parent
(root/'.build').mkdir(exist_ok=True)
(root/'.build/engine-build.txt').write_text(os.environ.get('GITHUB_SHA', 'development'))
triple=subprocess.check_output(['rustc','-vV'],text=True).split('host: ')[1].splitlines()[0]
subprocess.run([sys.executable,'-m','PyInstaller','--noconfirm','--clean','--onefile','--name','mara-engine',
 '--python-option','X utf8',
 '--add-data',str(root/'.build/engine-build.txt')+os.pathsep+'.',
 '--distpath',str(root/'.build/engine'),'--workpath',str(root/'.build/pyinstaller'),'--specpath',str(root/'.build'),
 '--paths',str(root),'--collect-submodules','mixmatch','--collect-data','mixmatch','--collect-all','imageio_ffmpeg',
 '--collect-submodules','keyring.backends','--copy-metadata','keyring','--collect-all','soundfile','--collect-all','pyloudnorm','--copy-metadata','anthropic',
 '--exclude-module','torch','--exclude-module','transformers','--exclude-module','pytest','--exclude-module','matplotlib',
 '--add-data',str(root/'remote_script')+os.pathsep+'remote_script','--add-data',str(root/'device')+os.pathsep+'device',
 str(root/'desktop/engine_entry.py')],cwd=root,check=True)
suffix='.exe' if sys.platform=='win32' else ''
target=root/'desktop/src-tauri/binaries'/f'mara-engine-{triple}{suffix}'
target.parent.mkdir(parents=True,exist_ok=True)
shutil.copy2(root/'.build/engine'/f'mara-engine{suffix}',target)
print(target)
