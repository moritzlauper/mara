import json
from mixmatch import progress

def point(done,total,t):return 'progress '+json.dumps(dict(done=done,total=total,t=t))

def test_resume_ignores_pause_and_restored_units(tmp_path):
    lines=['Taps:',point(0,100,100),point(50,100,3100)]
    first=progress.presentation(tmp_path,'run',lines)
    assert first['percent']==50 and first['eta_minutes']==50
    lines+=['Taps:',point(0,110,10000),point(50,110,10001),point(51,110,10061)]
    resumed=progress.presentation(tmp_path,'run',lines)
    assert resumed['percent']==50  # expanded work budget never moves backwards
    assert 49<=resumed['eta_minutes']<=52  # downtime and restored work excluded
    assert progress.presentation(tmp_path,'run',lines)==resumed  # polling never changes ETA
    assert progress.presentation(tmp_path,'run',lines,finished=True)['percent']==100
    assert progress.presentation(tmp_path,'new',['Taps:',point(0,100,20000)])['percent']==0
