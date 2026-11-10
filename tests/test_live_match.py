import json
import numpy as np
import soundfile as sf
from pathlib import Path
from mixmatch import preview, live_match, ab
from mixmatch.sim import demo_project


def test_match_attenuates_both_paths_and_requires_identical_passage(tmp_path):
    _,_,_,live=demo_project()
    snap=ab.save(live.get_set(),str(tmp_path),'2026','before')
    rate=48000
    wave=.1*np.sin(2*np.pi*440*np.arange(rate*2)/rate)
    sf.write(preview.audio_path(snap),wave,rate,subtype='PCM_24')
    Path(preview.metadata_path(snap)).write_text(json.dumps({'start_bar':99,'bars':16,'seconds':2,'peak_dbfs':-20}))
    measured={'target_lufs':-7.2,'explain':{'loudness':'2.2 LU quieter than the reference'},
              'measurement_groups':[{'section':'Kick, bars 99–115'}]}
    state={'loop':{'start':392,'length':64}}
    match=live_match.calculate(str(tmp_path),measured,state,normal_db=-3)
    assert match['ready'] and match['approximate']
    assert match['before_gain_db']<=0 and match['current_fader_db']<=-3
    rendered=preview.listening_file(str(tmp_path),'2026-before',match['target_lufs'])
    stat=Path(rendered).stat()
    actual=preview._loudness(rendered,stat.st_mtime_ns,stat.st_size)
    assert abs(actual-match['target_lufs'])<.02
    assert preview.listening_file(str(tmp_path),'2026-before',match['before_lufs']+3) is None
    state['loop']['start']=0
    assert not live_match.calculate(str(tmp_path),measured,state)['ready']
