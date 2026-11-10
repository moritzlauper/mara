"""Match recorded Before to Current using the last measured, identical Live passage.

Live tap loudness is a short-term estimate. Both paths use constant attenuation;
this does not compress the mix or boost the preview into clipping.
"""
import math
import os
import re
from urllib.parse import urlencode
from . import preview


def calculate(folder, state, live_state, normal_db=0.0):
    before=preview.available(folder)['before']
    if not before:
        return {'ready':False,'reason':'The Before recording is not ready.'}
    groups=state.get('measurement_groups',[])
    latest=groups[-1] if groups else {}
    bars=re.search(r'bars ([\d.]+)[–-]([\d.]+)',latest.get('section',''))
    expected=(before['start_bar'],before['start_bar']+before['bars'])
    loop=live_state.get('loop',{})
    if not bars or tuple(map(float,bars.groups()))!=expected or abs(loop.get('start',-100)/4+1-expected[0])>.01 or abs(loop.get('length',0)/4-before['bars'])>.01:
        return {'ready':False,'reason':'Live is on another passage. Matching resumes on the Before passage.'}
    target=state.get('target_lufs')
    diff=re.search(r'([\d.]+) LU (quieter|louder)',state.get('explain',{}).get('loudness',''))
    if target is None or not diff:
        return {'ready':False,'reason':'Waiting for a Current loudness measurement.'}
    current=float(target)+float(diff[1])*(-1 if diff[2]=='quieter' else 1)
    path=preview.selected_file(folder,'before',before['id'])
    st=os.stat(path);original=preview._loudness(path,st.st_mtime_ns,st.st_size)
    if original is None or not math.isfinite(current) or not math.isfinite(normal_db):
        return {'ready':False,'reason':'No usable loudness measurement.'}
    level=min(original,current+normal_db)
    return {'ready':True,'approximate':True,'target_lufs':level,'current_lufs':current,
            'before_lufs':original,'before_gain_db':level-original,'current_fader_db':level-current,
            'before_url':'/api/preview-audio?'+urlencode({'which':'before','id':before['id'],'listen_target':f'{level:.4f}'})}
