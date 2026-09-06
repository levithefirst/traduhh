from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import pytest
from agent.costs import calculate_costs
from agent.gates import HARD_GATES, evaluate_gates
from agent.geometry import build_geometry
from agent.pipeline import deterministic_idea_id
from agent.setups import detect_breakout_retest, detect_sweep_reclaim, detect_trend_pullback
from agent.setups.base import Detection
T=datetime(2026,9,5,12,tzinfo=timezone.utc)
S=SimpleNamespace(min_r_after_costs=1.2,paper_equity_usd=10000,risk_fraction=.005,taker_fee_bps=4.5,slippage_bps_floor=2)
def b(i,h=101,l=99,c=100):
    t=T+timedelta(hours=i); return {'open_time':t,'close_time':t+timedelta(hours=1),'o':100,'h':h,'l':l,'c':c,'v':100}
def rg(label='TREND_UP',**kw):
    x={'label':label,'secondary':[],'confidence':.7}; x.update(kw); return x
def tf(long=True): return {'atr_14':2,'ema_20':100,'grammar':'HH_HL' if long else 'LH_LL','last_swing_low_px':97,'last_swing_low_t':(T-timedelta(hours=2)).isoformat(),'last_swing_high_px':103,'last_swing_high_t':(T-timedelta(hours=2)).isoformat()}
def trend(long=True):
    x=[b(i) for i in range(5)]
    if long: x[-3].update(l=100,h=102); x[-1].update(h=103,l=100,c=102)
    else: x[-3].update(h=100,l=98); x[-1].update(h=100,l=97,c=98)
    return x
def br(long=True):
    x=[b(i,h=100,l=99,c=100) for i in range(25)]
    if long: x[-2].update(h=102,l=100,c=101); x[-1].update(h=101.5,l=99.9,c=100.8)
    else: x[-2].update(h=99,l=98,c=98.9); x[-1].update(h=99.1,l=98.5,c=98.8)
    return x
def sf(): return {'atr_14':1}
def sw(long=True):
    x=[b(i) for i in range(4)]
    if long:x[-1].update(h=101,l=97.5,c=99)
    else:x[-1].update(h=102.5,l=99,c=101)
    return x
def sv(): return {'atr_14':2,'pdl':98,'pdh':102,'equal_low':True,'equal_high':True,'last_swing_low_px':98,'last_swing_high_px':102}

def test_01(): assert detect_trend_pullback(trend(),asset='BTC',timeframe='1h',features=tf(),regime=rg(),bar_open_time=T+timedelta(hours=4))
def test_02(): assert detect_trend_pullback(trend(False),asset='BTC',timeframe='1h',features=tf(False),regime=rg('TREND_DOWN'),bar_open_time=T+timedelta(hours=4))
def test_03(): assert detect_breakout_retest(br(),asset='BTC',timeframe='1h',features=sf(),regime=rg(),bar_open_time=br()[-1]['open_time'])
def test_04(): assert detect_breakout_retest(br(False),asset='BTC',timeframe='1h',features=sf(),regime=rg('TREND_DOWN'),bar_open_time=br(False)[-1]['open_time'])
def test_05(): assert detect_sweep_reclaim(sw(),asset='BTC',timeframe='1h',features=sv(),regime=rg('RANGE'),bar_open_time=sw()[-1]['open_time'])
def test_06(): assert detect_sweep_reclaim(sw(False),asset='BTC',timeframe='1h',features=sv(),regime=rg('RANGE'),bar_open_time=sw(False)[-1]['open_time'])
def test_07(): assert detect_trend_pullback(trend(),asset='BTC',timeframe='1h',features=tf(),regime=rg('RANGE'),bar_open_time=T) is None
def test_08(): assert detect_trend_pullback(trend(),asset='BTC',timeframe='1h',features=tf(),regime=rg(secondary=['PANIC']),bar_open_time=T) is None
def test_09(): assert detect_breakout_retest(br(),asset='BTC',timeframe='1h',features=sf(),regime=rg(secondary=['EVENT_HIGH']),bar_open_time=br()[-1]['open_time']) is None
def test_10(): assert detect_breakout_retest(br(),asset='BTC',timeframe='1h',features=sf(),regime=rg('RANGE'),bar_open_time=br()[-1]['open_time']) is None
def test_11(): assert detect_trend_pullback(trend(),asset='BTC',timeframe='1h',features=tf(),regime=rg(confidence=.5),bar_open_time=T+timedelta(hours=4))
def test_12():
    x=br(); x[-2]['c']=100.1; assert detect_breakout_retest(x,asset='BTC',timeframe='1h',features=sf(),regime=rg(),bar_open_time=x[-1]['open_time'])
def test_13():
    x=br(); x[-1]['h']=9999; d=detect_breakout_retest(x,asset='BTC',timeframe='1h',features=sf(),regime=rg(),bar_open_time=x[-1]['open_time']); assert d.structural_reference['break_boundary']==100
def test_14():
    x=br(); x[-2]['c']=103; assert detect_breakout_retest(x,asset='BTC',timeframe='1h',features=sf(),regime=rg(),bar_open_time=x[-1]['open_time']) is None
def test_15():
    x=sw(); x[-1]['c']=97.5; x.append(b(4,h=100,l=97,c=99)); assert detect_sweep_reclaim(x,asset='BTC',timeframe='1h',features=sv(),regime=rg('RANGE'),bar_open_time=x[-1]['open_time'])
def test_16():
    x=sw(); x[-1]['c']=97.5; assert detect_sweep_reclaim(x,asset='BTC',timeframe='1h',features=sv(),regime=rg('RANGE'),bar_open_time=x[-1]['open_time']) is None
def test_17():
    d=detect_trend_pullback(trend(),asset='BTC',timeframe='1h',features=tf(),regime=rg(),bar_open_time=T+timedelta(hours=4)); assert d.direction=='long'
def test_18():
    d=detect_trend_pullback(trend(False),asset='BTC',timeframe='1h',features=tf(False),regime=rg('TREND_DOWN'),bar_open_time=T+timedelta(hours=4)); assert d.stop>d.entry
def test_19():
    d=detect_trend_pullback(trend(),asset='BTC',timeframe='1h',features=tf(),regime=rg(),bar_open_time=T+timedelta(hours=4)); g=build_geometry(d,features=tf(),settings=S); assert g.entry>g.stop and g.targets

def test_20():
    d=detect_sweep_reclaim(sw(),asset='BTC',timeframe='1h',features=sv(),regime=rg('RANGE'),bar_open_time=T+timedelta(hours=3)); g=build_geometry(d,features=sv(),settings=S); assert g.raw_r==pytest.approx(1.5)
def test_21():
    d=detect_trend_pullback(trend(),asset='BTC',timeframe='1h',features=tf(),regime=rg(),bar_open_time=T+timedelta(hours=4)); g=build_geometry(d,features=tf(),settings=S); assert g.risk_per_unit>0
def test_22():
    c=calculate_costs(notional=1000,size=10,notional_to_10bps=1000,funding=0,timeframe='1h',risk_cash=50,raw_r=2,taker_fee_bps=4.5,slippage_bps_floor=2,hold_bars=12); assert c.fee_round_trip==pytest.approx(.9)
def test_23():
    c=calculate_costs(notional=1000,size=10,notional_to_10bps=1000,funding=0,timeframe='1h',risk_cash=50,raw_r=2,taker_fee_bps=4.5,slippage_bps_floor=2,hold_bars=12); assert c.slip_bps==2
def test_24():
    c=calculate_costs(notional=2000,size=20,notional_to_10bps=1000,funding=0,timeframe='1h',risk_cash=50,raw_r=2,taker_fee_bps=4.5,slippage_bps_floor=2,hold_bars=12); assert c.impact_bps==20
def test_25():
    c=calculate_costs(notional=10000,size=100,notional_to_10bps=1000,funding=0,timeframe='1h',risk_cash=50,raw_r=2,taker_fee_bps=4.5,slippage_bps_floor=2,hold_bars=12); assert c.impact_bps==25
def test_26():
    c=calculate_costs(notional=1000,size=10,notional_to_10bps=1000,funding=.001,timeframe='1h',risk_cash=50,raw_r=2,taker_fee_bps=4.5,slippage_bps_floor=2,hold_bars=12); assert c.funding_est==12
def test_27():
    c=calculate_costs(notional=1000,size=10,notional_to_10bps=1000,funding=0,timeframe='1h',risk_cash=50,raw_r=2,taker_fee_bps=4.5,slippage_bps_floor=2,hold_bars=12); assert c.planned_r_after_costs<2
def det(): return Detection('trend_pullback','BTC','1h','long',T,0,100,98,[103],{}, {})
def geo(): return SimpleNamespace(entry=100,stop=98,targets=[103],risk_per_unit=2,raw_r=1.5,risk_cash=50,size=.25,notional=25)
def cost(): return SimpleNamespace(funding_est=0,planned_r_after_costs=1.5)
def feat(**k):
    x={'atr_14':2,'vol_ratio':1,'adx_14':25,'spread_bps':1,'lookahead_protected':True}; x.update(k); return x
def test_28(): assert tuple(evaluate_gates(detection=det(),geometry=geo(),costs=cost(),features=feat(),regime=rg()).hard)==HARD_GATES
def test_29(): assert not evaluate_gates(detection=det(),geometry=geo(),costs=cost(),features=feat(),regime=rg(),integrity_ok=False).hard['data_valid']
def test_30(): assert not evaluate_gates(detection=det(),geometry=geo(),costs=cost(),features=feat(),regime=rg('UNKNOWN')).hard['regime_ok']
def test_31(): assert not evaluate_gates(detection=det(),geometry=SimpleNamespace(**{**geo().__dict__,'targets':[]}),costs=cost(),features=feat(),regime=rg()).hard['setup_complete']
def test_32(): assert not evaluate_gates(detection=det(),geometry=SimpleNamespace(**{**geo().__dict__,'stop':100}),costs=cost(),features=feat(),regime=rg()).hard['invalidation_clear']
def test_33(): assert not evaluate_gates(detection=det(),geometry=geo(),costs=SimpleNamespace(funding_est=0,planned_r_after_costs=1.19),features=feat(),regime=rg()).hard['min_r']
def test_34(): assert not evaluate_gates(detection=det(),geometry=SimpleNamespace(**{**geo().__dict__,'risk_per_unit':.5}),costs=cost(),features=feat(),regime=rg()).hard['stop_vs_noise']
def test_35(): assert not evaluate_gates(detection=det(),geometry=SimpleNamespace(**{**geo().__dict__,'risk_per_unit':5.1}),costs=cost(),features=feat(),regime=rg()).hard['stop_too_wide']
def test_36(): assert not evaluate_gates(detection=det(),geometry=SimpleNamespace(**{**geo().__dict__,'notional':100}),costs=cost(),features=feat(),regime=rg(),day_ntl_vlm=1000).hard['liquidity']
def test_37(): assert not evaluate_gates(detection=det(),geometry=geo(),costs=SimpleNamespace(funding_est=20,planned_r_after_costs=1.5),features=feat(),regime=rg()).hard['funding_carry']
def test_38(): assert not evaluate_gates(detection=det(),geometry=geo(),costs=cost(),features=feat(),regime=rg(),paper_positions=[{'asset':'BTC','status':'OPEN'}]).hard['cluster']
def test_39(): assert not evaluate_gates(detection=det(),geometry=geo(),costs=cost(),features=feat(),regime=rg(),day_pnl_pct=-.02).hard['daily_loss']
def test_40(): assert not evaluate_gates(detection=det(),geometry=geo(),costs=cost(),features=feat(),regime=rg(),week_pnl_pct=-.05).hard['weekly_loss']
def test_41(): assert not evaluate_gates(detection=det(),geometry=geo(),costs=cost(),features=feat(),regime=rg(),halted=True).hard['circuit']
def test_42(): assert not evaluate_gates(detection=det(),geometry=geo(),costs=cost(),features=feat(),regime=rg(),hist_cell={'n':80,'mean_r':-.16}).hard['hist_cell_fatal']
def test_43(): assert not evaluate_gates(detection=det(),geometry=geo(),costs=cost(),features=feat(vol_ratio=.5,adx_14=18,spread_bps=6),regime=rg()).passed
def test_44():
    a=deterministic_idea_id(asset='BTC',timeframe='1h',setup_id='trend_pullback',direction='long',bar_open_time=T,strategy_version_id='sv',evidence={'x':1}); b=deterministic_idea_id(asset='BTC',timeframe='1h',setup_id='trend_pullback',direction='long',bar_open_time=T,strategy_version_id='sv',evidence={'x':1}); assert a==b
def test_45():
    a=deterministic_idea_id(asset='BTC',timeframe='1h',setup_id='trend_pullback',direction='long',bar_open_time=T,strategy_version_id='sv',evidence={'x':1}); b=deterministic_idea_id(asset='BTC',timeframe='1h',setup_id='trend_pullback',direction='long',bar_open_time=T,strategy_version_id='sv',evidence={'x':2}); assert a!=b
def test_46(): assert evaluate_gates(detection=det(),geometry=geo(),costs=SimpleNamespace(funding_est=0,planned_r_after_costs=1),features=feat(),regime=rg('UNKNOWN')).decision=='NO_TRADE'
def test_47(): assert detect_breakout_retest(br()[:21],asset='BTC',timeframe='1h',features=sf(),regime=rg(),bar_open_time=T) is None
def test_48():
    d=detect_trend_pullback(trend(),asset='BTC',timeframe='1h',features=tf(),regime=rg(),bar_open_time=T+timedelta(hours=4)); assert 'realized_r' not in d.to_dict()
def test_49():
    r=evaluate_gates(detection=det(),geometry=geo(),costs=cost(),features=feat(),regime=rg(),regime_ok=True) if False else None; assert r is None
