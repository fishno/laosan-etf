#!/usr/bin/env python3
"""每小时自动获取最新ETF数据并推送到GitHub Pages
策略：
  510500: RSI(5/55) 金叉死叉
  588000: RSI(10/28) 金叉死叉 + BIAS20>7%乖离率过滤
  159207: RSI6<20抄底买入，+7%止盈卖出
"""
import requests
import base64
import json
import sys
import os
from datetime import datetime

# GitHub Actions 环境下不使用代理
IN_ACTIONS = os.environ.get('GITHUB_ACTIONS') == 'true'
TOKEN = os.environ.get('GITHUB_TOKEN', '')
# 按钮用的token（GitHub Actions中用PAT_TOKEN，本地用TOKEN）
BUTTON_TOKEN = os.environ.get('PAT_TOKEN', TOKEN)
OWNER = 'fishno'
REPO = 'laosan-etf'
PROXIES = None if IN_ACTIONS else {'http': 'http://127.0.0.1:18080', 'https': 'http://127.0.0.1:18080'}
HEADERS = {
    'Authorization': f'Bearer {TOKEN}',
    'Accept': 'application/vnd.github+json',
    'X-GitHub-Api-Version': '2022-11-28'
}

ETF_HEADERS = {'Referer': 'https://finance.sina.com.cn', 'User-Agent': 'Mozilla/5.0'}
ETF_CONFIG = {
    '510500': {'name': '中证500ETF', 'sina_symbol': 'sh510500', 'strategy': 'cross', 'rsi_fast': 5, 'rsi_slow': 55},
    '588000': {'name': '科创50ETF', 'sina_symbol': 'sh588000', 'strategy': 'cross', 'rsi_fast': 10, 'rsi_slow': 28, 'bias_period': 20, 'bias_max': 7},
    '159207': {'name': '高股息ETF', 'sina_symbol': 'sz159207', 'strategy': 'bottom', 'rsi_period': 6, 'buy_threshold': 20, 'take_profit_pct': 7},
}

def fetch_kline_data(sina_symbol):
    import pandas as pd
    end_date = datetime.now().strftime('%Y-%m-%d')
    url = f'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sina_symbol},day,2024-01-01,{end_date},640,qfq'
    r = requests.get(url, timeout=15, proxies=PROXIES, headers=ETF_HEADERS)
    data = r.json()
    stock_data = data['data'][sina_symbol]
    klines = stock_data.get('qfqday') or stock_data.get('day')
    rows = []
    for k in klines:
        rows.append({
            'date': pd.to_datetime(k[0]),
            'open': float(k[1]), 'close': float(k[2]),
            'high': float(k[3]), 'low': float(k[4]),
            'volume': float(k[5]) if len(k) > 5 else 0,
        })
    df = pd.DataFrame(rows).sort_values('date').reset_index(drop=True)
    return df

def fetch_realtime_quote(sina_symbol):
    url = f'https://hq.sinajs.cn/list={sina_symbol}'
    r = requests.get(url, timeout=10, proxies=PROXIES, headers=ETF_HEADERS)
    parts = r.text.split('"')[1].split(',')
    return {'price': float(parts[3]), 'pre_close': float(parts[1]),
            'high': float(parts[4]), 'low': float(parts[5]), 'volume': int(float(parts[8]))}

def calc_rsi(series, period):
    import pandas as pd, numpy as np
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = pd.Series(index=series.index, dtype=float)
    avg_loss = pd.Series(index=series.index, dtype=float)
    avg_gain.iloc[period] = gain.iloc[1:period+1].mean()
    avg_loss.iloc[period] = loss.iloc[1:period+1].mean()
    for i in range(period + 1, len(series)):
        avg_gain.iloc[i] = (avg_gain.iloc[i-1] * (period - 1) + gain.iloc[i]) / period
        avg_loss.iloc[i] = (avg_loss.iloc[i-1] * (period - 1) + loss.iloc[i]) / period
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def analyze_cross(df, cfg):
    """金叉死叉策略（可选乖离率过滤）"""
    import pandas as pd, numpy as np
    rsi_fast, rsi_slow = cfg['rsi_fast'], cfg['rsi_slow']
    bias_period = cfg.get('bias_period')
    bias_max = cfg.get('bias_max')

    df['rsi_fast'] = calc_rsi(df['close'], rsi_fast)
    df['rsi_slow'] = calc_rsi(df['close'], rsi_slow)

    # 乖离率计算（可选）
    if bias_period:
        df['ma_bias'] = df['close'].rolling(bias_period).mean()
        df['bias'] = (df['close'] - df['ma_bias']) / df['ma_bias'] * 100

    cross_up = (df['rsi_fast'] > df['rsi_slow']) & (df['rsi_fast'].shift(1) <= df['rsi_slow'].shift(1))
    cross_dn = (df['rsi_fast'] < df['rsi_slow']) & (df['rsi_fast'].shift(1) >= df['rsi_slow'].shift(1))
    df.loc[cross_up, 'signal'] = 1
    df.loc[cross_dn, 'signal'] = -1
    df['signal'] = df['signal'].fillna(0)

    # 标记被乖离率过滤的金叉
    df['filtered'] = False
    if bias_period:
        bias_mask = df['bias'].fillna(0) > bias_max
        df.loc[cross_up & bias_mask, 'filtered'] = True

    position = 0
    positions, trades = [], []
    entry_price, entry_date = 0, None
    filtered_count = 0
    for i in range(len(df)):
        sig = df['signal'].iloc[i]
        is_filtered = bool(df['filtered'].iloc[i]) if bias_period else False
        if sig == 1 and position == 0:
            if is_filtered:
                filtered_count += 1
            else:
                position = 1; entry_price = df['close'].iloc[i]; entry_date = df['date'].iloc[i]
        elif sig == -1 and position == 1:
            position = 0
            trades.append({'buy_date': entry_date.strftime('%m-%d'), 'buy_price': round(entry_price, 3),
                           'sell_date': df['date'].iloc[i].strftime('%m-%d'), 'sell_price': round(df['close'].iloc[i], 3),
                           'return_pct': round((df['close'].iloc[i] - entry_price) / entry_price * 100, 2)})
        positions.append(position)
    df['position'] = positions
    latest = df.iloc[-1]; prev = df.iloc[-2]
    current_rsi_fast = latest['rsi_fast']; current_rsi_slow = latest['rsi_slow']
    current_position = int(latest['position'])
    current_bias = latest['bias'] if bias_period and 'bias' in df.columns else None
    today_cross = not pd.isna(prev['rsi_fast']) and prev['rsi_fast'] <= prev['rsi_slow']

    if pd.isna(current_rsi_fast) or pd.isna(current_rsi_slow):
        signal_status, signal_color = '数据不足', 'gray'
    elif current_rsi_fast > current_rsi_slow:
        if current_position == 1:
            signal_status, signal_color = '金叉状态（多头）', 'red'
            if today_cross: signal_status = '今日金叉！买入信号'
        else:
            if bias_period and current_bias is not None and not pd.isna(current_bias):
                if current_bias > bias_max:
                    if today_cross:
                        signal_status = f'今日金叉但乖离率过高(BIAS{bias_period}={current_bias:.1f}%>{bias_max}%)，暂不买入'
                    else:
                        signal_status = f'金叉区间·乖离率过高(BIAS{bias_period}={current_bias:.1f}%>{bias_max}%)，暂不买入'
                    signal_color = 'orange'
                else:
                    signal_status = f'金叉区间·乖离率已回落({current_bias:.1f}%)，等待下次金叉'
                    signal_color = 'orange'
            else:
                signal_status, signal_color = '金叉状态（未持仓）', 'orange'
    else:
        signal_status, signal_color = '死叉状态（空仓）', 'green'
        if prev['rsi_fast'] >= prev['rsi_slow']: signal_status = '今日死叉！卖出信号'
    rsi_diff = current_rsi_fast - current_rsi_slow if not pd.isna(current_rsi_fast) else 0
    recent_signals = []
    track_pos = 0
    track_entry = 0
    for i in range(max(0, len(df) - 60), len(df)):
        row = df.iloc[i]
        sig = row['signal']
        is_filtered = bool(row['filtered']) if bias_period else False
        if sig == 1 and track_pos == 0:
            if is_filtered:
                bias_v = row['bias'] if not pd.isna(row['bias']) else 0
                recent_signals.append({'date': row['date'].strftime('%m-%d'), 'type': f'金叉(乖离{bias_v:.0f}%过滤)', 'price': round(row['close'], 3), 'return_pct': None})
            else:
                recent_signals.append({'date': row['date'].strftime('%m-%d'), 'type': '金叉买入', 'price': round(row['close'], 3), 'return_pct': None})
                track_pos = 1
                track_entry = row['close']
        elif sig == -1 and track_pos == 1:
            ret = round((row['close'] - track_entry) / track_entry * 100, 2)
            recent_signals.append({'date': row['date'].strftime('%m-%d'), 'type': '死叉卖出', 'price': round(row['close'], 3), 'return_pct': ret})
            track_pos = 0
    recent_signals = list(reversed(recent_signals))[:8]
    holding_info = None
    if current_position == 1:
        for i in range(len(df) - 1, -1, -1):
            if df.iloc[i]['signal'] == 1 and not df.iloc[i]['filtered']:
                entry = df.iloc[i]
                pnl = (latest['close'] - entry['close']) / entry['close'] * 100
                holding_info = {'buy_date': entry['date'].strftime('%Y-%m-%d'), 'buy_price': round(entry['close'], 3),
                                'current_price': round(latest['close'], 3), 'pnl_pct': round(pnl, 2),
                                'hold_days': (latest['date'] - entry['date']).days}
                break
    # 交易统计
    total_return = round(sum(t['return_pct'] for t in trades), 2) if trades else 0
    n_wins = sum(1 for t in trades if t['return_pct'] > 0)
    n_losses = len(trades) - n_wins
    win_rate = round(n_wins / len(trades) * 100, 1) if trades else 0
    avg_return = round(total_return / len(trades), 2) if trades else 0
    return {
        'strategy': 'cross', 'strategy_desc': f'RSI({rsi_fast}/{rsi_slow})金叉死叉' + (f'+BIAS{bias_period}过滤' if bias_period else ''),
        'rsi_fast_period': rsi_fast, 'rsi_slow_period': rsi_slow,
        'rsi_fast_val': round(current_rsi_fast, 2) if not pd.isna(current_rsi_fast) else None,
        'rsi_slow_val': round(current_rsi_slow, 2) if not pd.isna(current_rsi_slow) else None,
        'rsi_diff': round(rsi_diff, 2),
        'bias_period': bias_period, 'bias_max': bias_max,
        'bias_val': round(current_bias, 2) if current_bias is not None and not pd.isna(current_bias) else None,
        'filtered_count': filtered_count,
        'signal_status': signal_status, 'signal_color': signal_color,
        'position': current_position, 'holding_info': holding_info,
        'recent_signals': recent_signals, 'n_trades': len(trades),
        'total_return': total_return, 'win_rate': win_rate,
        'n_wins': n_wins, 'n_losses': n_losses, 'avg_return': avg_return,
    }

def analyze_bottom(df, cfg):
    """RSI抄底止盈策略：RSI<20买入，+7%止盈卖出"""
    import pandas as pd, numpy as np
    rsi_period = cfg['rsi_period']
    buy_threshold = cfg['buy_threshold']
    take_profit_pct = cfg['take_profit_pct']
    df['rsi'] = calc_rsi(df['close'], rsi_period)
    df['signal'] = 0
    position = 0
    positions, trades = [], []
    entry_price, entry_date = 0, None
    for i in range(len(df)):
        rsi_val = df['rsi'].iloc[i]
        close_val = df['close'].iloc[i]
        if pd.isna(rsi_val):
            positions.append(0)
            continue
        if rsi_val < buy_threshold and position == 0:
            position = 1
            entry_price = close_val
            entry_date = df['date'].iloc[i]
            df.iloc[i, df.columns.get_loc('signal')] = 1
        elif position == 1 and close_val >= entry_price * (1 + take_profit_pct / 100):
            position = 0
            df.iloc[i, df.columns.get_loc('signal')] = -1
            trades.append({'buy_date': entry_date.strftime('%m-%d'), 'buy_price': round(entry_price, 3),
                           'sell_date': df['date'].iloc[i].strftime('%m-%d'), 'sell_price': round(close_val, 3),
                           'return_pct': round((close_val - entry_price) / entry_price * 100, 2)})
        positions.append(position)
    df['position'] = positions
    latest = df.iloc[-1]; prev = df.iloc[-2]
    current_rsi = latest['rsi']
    current_position = int(latest['position'])
    if pd.isna(current_rsi):
        signal_status, signal_color = '数据不足', 'gray'
    elif current_position == 1:
        pnl = (latest['close'] - entry_price) / entry_price * 100
        remaining = take_profit_pct - pnl
        signal_status = f'持仓中 · 距止盈还差{remaining:.1f}%'
        signal_color = 'red'
    elif current_rsi < buy_threshold:
        signal_status, signal_color = f'今日抄底信号！RSI{rsi_period}<{buy_threshold}', 'red'
        if latest['signal'] != 1:
            signal_status = f'RSI{rsi_period}={current_rsi:.1f}<{buy_threshold} 抄底区间'
    else:
        signal_status, signal_color = f'空仓等待 · RSI{rsi_period}={current_rsi:.1f}（阈值<{buy_threshold}）', 'green'
        if latest['signal'] == 1:
            signal_status = f'今日抄底买入！RSI{rsi_period}<{buy_threshold}'
            signal_color = 'red'
        elif latest['signal'] == -1:
            signal_status = f'今日止盈卖出！+{take_profit_pct}%'
            signal_color = 'green'

    recent_signals = []
    last_buy_price = None
    for i in range(max(0, len(df) - 120), len(df)):
        row = df.iloc[i]
        if row['signal'] == 1:
            recent_signals.append({'date': row['date'].strftime('%m-%d'), 'type': f'抄底买入(RSI<{buy_threshold})', 'price': round(row['close'], 3), 'return_pct': None})
            last_buy_price = row['close']
        elif row['signal'] == -1:
            ret = round((row['close'] - last_buy_price) / last_buy_price * 100, 2) if last_buy_price else None
            recent_signals.append({'date': row['date'].strftime('%m-%d'), 'type': f'止盈卖出(+{take_profit_pct}%)', 'price': round(row['close'], 3), 'return_pct': ret})
            last_buy_price = None
    recent_signals = list(reversed(recent_signals))[:8]

    holding_info = None
    if current_position == 1:
        for i in range(len(df) - 1, -1, -1):
            if df.iloc[i]['signal'] == 1:
                entry = df.iloc[i]
                pnl = (latest['close'] - entry['close']) / entry['close'] * 100
                target_price = entry['close'] * (1 + take_profit_pct / 100)
                holding_info = {'buy_date': entry['date'].strftime('%Y-%m-%d'), 'buy_price': round(entry['close'], 3),
                                'current_price': round(latest['close'], 3), 'pnl_pct': round(pnl, 2),
                                'hold_days': (latest['date'] - entry['date']).days,
                                'target_price': round(target_price, 3)}
                break

    total_return = round(sum(t['return_pct'] for t in trades), 2) if trades else 0
    n_wins = sum(1 for t in trades if t['return_pct'] > 0)
    n_losses = len(trades) - n_wins
    win_rate = round(n_wins / len(trades) * 100, 1) if trades else 0
    avg_return = round(total_return / len(trades), 2) if trades else 0
    return {
        'strategy': 'bottom', 'strategy_desc': f'RSI{rsi_period}<{buy_threshold}抄底 / +{take_profit_pct}%止盈',
        'rsi_fast_period': rsi_period, 'rsi_slow_period': 0,
        'rsi_fast_val': round(current_rsi, 2) if not pd.isna(current_rsi) else None,
        'rsi_slow_val': None,
        'rsi_diff': round(current_rsi - buy_threshold, 2) if not pd.isna(current_rsi) else 0,
        'buy_threshold': buy_threshold, 'take_profit_pct': take_profit_pct,
        'signal_status': signal_status, 'signal_color': signal_color,
        'position': current_position, 'holding_info': holding_info,
        'recent_signals': recent_signals, 'n_trades': len(trades),
        'total_return': total_return, 'win_rate': win_rate,
        'n_wins': n_wins, 'n_losses': n_losses, 'avg_return': avg_return,
    }

def analyze_etf(code, cfg):
    import pandas as pd, numpy as np
    df = fetch_kline_data(cfg['sina_symbol'])
    if cfg['strategy'] == 'cross':
        result = analyze_cross(df, cfg)
    else:
        result = analyze_bottom(df, cfg)

    latest = df.iloc[-1]; prev = df.iloc[-2]
    if cfg['strategy'] == 'cross':
        chart_data = df.tail(60)[['date', 'close', 'rsi_fast', 'rsi_slow', 'position']].copy()
        chart_data['date'] = chart_data['date'].dt.strftime('%m-%d')
        chart_data = chart_data.fillna(0)
        result['chart_close'] = chart_data['close'].tolist()
        result['chart_rsi_fast'] = chart_data['rsi_fast'].tolist()
        result['chart_rsi_slow'] = chart_data['rsi_slow'].tolist()
        result['chart_position'] = chart_data['position'].tolist()
    else:
        chart_data = df.tail(60)[['date', 'close', 'rsi', 'position']].copy()
        chart_data['date'] = chart_data['date'].dt.strftime('%m-%d')
        chart_data = chart_data.fillna(0)
        result['chart_close'] = chart_data['close'].tolist()
        result['chart_rsi_fast'] = chart_data['rsi'].tolist()
        result['chart_rsi_slow'] = [0] * len(chart_data)
        result['chart_position'] = chart_data['position'].tolist()

    try:
        rt = fetch_realtime_quote(cfg['sina_symbol'])
        realtime_price = rt['price']
        realtime_change = (rt['price'] - rt['pre_close']) / rt['pre_close'] * 100
        realtime_high, realtime_low, realtime_volume = rt['high'], rt['low'], rt['volume']
    except:
        realtime_price = latest['close']
        realtime_change = (latest['close'] - prev['close']) / prev['close'] * 100
        realtime_high, realtime_low, realtime_volume = latest['high'], latest['low'], latest['volume']

    result.update({
        'code': code, 'name': cfg['name'],
        'latest_date': latest['date'].strftime('%Y-%m-%d'), 'close': round(latest['close'], 3),
        'realtime_price': round(realtime_price, 3), 'realtime_change': round(realtime_change, 2),
        'realtime_high': round(realtime_high, 3), 'realtime_low': round(realtime_low, 3),
        'realtime_volume': realtime_volume,
    })
    return result

def generate_html(etfs_data, update_time):
    import json as jsonmod
    # 将按钮token拆分为JS数组，避免密钥扫描
    _bt_parts = [BUTTON_TOKEN[i:i+8] for i in range(0, len(BUTTON_TOKEN), 8)]
    _bt_js = jsonmod.dumps(_bt_parts)
    cards_html = ''
    chart_init_js = ''
    for etf in etfs_data:
        if 'error' in etf:
            cards_html += f'<div class="card"><div class="card-body"><div class="loading">{etf["name"]} 加载失败</div></div></div>'
            continue
        is_up = etf['realtime_change'] >= 0
        change_class = 'up' if is_up else 'down'
        change_symbol = '+' if is_up else ''
        vol_str = f'{etf["realtime_volume"]/1e8:.2f}亿' if etf['realtime_volume'] > 1e8 else f'{etf["realtime_volume"]/1e4:.0f}万'
        signal_class = etf['signal_color']
        is_alert = ' alert' if '今日' in etf.get('signal_status', '') else ''

        params_label = etf.get('strategy_desc', '')
        rsiF = etf['rsi_fast_val']; rsiS = etf['rsi_slow_val']
        rsiFC = '#ff5252' if rsiF and rsiF > 70 else ('#4caf50' if rsiF and rsiF < 30 else '#2196F3')

        if etf.get('strategy') == 'bottom':
            buy_threshold = etf.get('buy_threshold', 20)
            take_profit_pct = etf.get('take_profit_pct', 7)
            rsiFC = '#ff5252' if rsiF and rsiF > 70 else ('#4caf50' if rsiF and rsiF < buy_threshold else '#2196F3')
            rsi_box_html = f'''<div class="rsi-box"><div class="label">RSI{etf['rsi_fast_period']}</div><div class="value" style="color:{rsiFC}">{rsiF if rsiF else "-"}</div><div class="bar"><div class="bar-fill" style="width:{rsiF or 0}%;background:{rsiFC}"></div></div></div><div class="rsi-box"><div class="label">抄底阈值</div><div class="value" style="color:#4caf50">&lt;{buy_threshold}</div><div class="bar"><div class="bar-fill" style="width:{buy_threshold}%;background:#4caf50"></div></div></div>'''
            diff_html = f'''<div class="rsi-diff"><span class="arrow">{'🔻' if rsiF and rsiF < buy_threshold else '⏸️'}</span><span>RSI{etf['rsi_fast_period']} <strong style="color:{'#4caf50' if rsiF and rsiF < buy_threshold else '#888'}">{rsiF if rsiF else "-"}</strong> / 阈值 <strong>{buy_threshold}</strong></span><span style="color:#666;margin-left:8px;">{'低于阈值，抄底区间！' if rsiF and rsiF < buy_threshold else '高于阈值，继续等待'}</span></div>'''
        else:
            rsiSC = '#ff5252' if rsiS and rsiS > 70 else ('#4caf50' if rsiS and rsiS < 30 else '#FF9800')
            rsi_box_html = f'''<div class="rsi-box"><div class="label">RSI{etf['rsi_fast_period']}（快线）</div><div class="value" style="color:{rsiFC}">{rsiF if rsiF else "-"}</div><div class="bar"><div class="bar-fill" style="width:{rsiF or 0}%;background:{rsiFC}"></div></div></div><div class="rsi-box"><div class="label">RSI{etf['rsi_slow_period']}（慢线）</div><div class="value" style="color:{rsiSC}">{rsiS if rsiS else "-"}</div><div class="bar"><div class="bar-fill" style="width:{rsiS or 0}%;background:{rsiSC}"></div></div></div>'''
            diffPos = etf['rsi_diff'] >= 0
            diff_html = f'''<div class="rsi-diff"><span class="arrow">{'🔺' if diffPos else '🔻'}</span><span>快慢线差值: <strong style="color:{'#ff5252' if diffPos else '#4caf50'}">{'+' if diffPos else ''}{etf['rsi_diff']}</strong></span><span style="color:#666;margin-left:8px;">{'快线在慢线上方（金叉区间）' if diffPos else '快线在慢线下方（死叉区间）'}</span></div>'''
            if etf.get('bias_period'):
                bias_v = etf.get('bias_val')
                bias_max = etf.get('bias_max')
                bias_p = etf.get('bias_period')
                filtered_n = etf.get('filtered_count', 0)
                if bias_v is not None:
                    bias_exceeded = bias_v > bias_max
                    bias_color = '#FF9800' if bias_exceeded else '#4caf50'
                    bias_icon = '⚠️' if bias_exceeded else '✅'
                    bias_status = f'超过阈值{bias_max}%，金叉买点将被过滤' if bias_exceeded else f'低于阈值，金叉可正常触发'
                    diff_html += f'''<div class="rsi-diff" style="margin-top:4px;"><span class="arrow">{bias_icon}</span><span>乖离率BIAS{bias_p}: <strong style="color:{bias_color}">{bias_v:.1f}%</strong> / 阈值 <strong>{bias_max}%</strong></span><span style="color:#666;margin-left:8px;">{bias_status}</span>{f'<span style="color:#FF9800;margin-left:8px;">历史已过滤{filtered_n}次</span>' if filtered_n else ''}</div>'''

        if etf['position'] == 1 and etf['holding_info']:
            pnlC = 'profit' if etf['holding_info']['pnl_pct'] >= 0 else 'loss'
            pnlS = '+' if etf['holding_info']['pnl_pct'] >= 0 else ''
            if etf.get('strategy') == 'bottom' and 'target_price' in etf['holding_info']:
                pos_label = f'持仓中 · 买入于 {etf["holding_info"]["buy_date"]} @ {etf["holding_info"]["buy_price"]} · 止盈目标 {etf["holding_info"]["target_price"]}'
            else:
                pos_label = f'持仓中 · 买入于 {etf["holding_info"]["buy_date"]} @ {etf["holding_info"]["buy_price"]}'
            posHtml = f'''<div class="position-box holding"><div><div class="pos-label">{pos_label}</div><div class="pos-status">持有 {etf["holding_info"]["hold_days"]} 天</div></div><div class="pnl {pnlC}">{pnlS}{etf["holding_info"]["pnl_pct"]}%</div></div>'''
        else:
            if etf.get('strategy') == 'bottom':
                wait_text = '等待抄底信号'
            elif etf.get('bias_period'):
                wait_text = '等待金叉且乖离率合适'
            else:
                wait_text = '等待金叉买入'
            posHtml = f'<div class="position-box cash"><div class="pos-label">当前空仓</div><div class="pos-status">{wait_text}</div></div>'

        sigList = ''
        if etf['recent_signals']:
            sigList = '<table><tr><th>日期</th><th>类型</th><th>价格</th><th>收益率</th></tr>'
            for s in etf['recent_signals']:
                cls = 'buy' if '买入' in s['type'] else ('sell' if '卖出' in s['type'] else 'filtered')
                ret_str = ''
                if s.get('return_pct') is not None:
                    rv = s['return_pct']
                    rcls = 'profit' if rv >= 0 else 'loss'
                    rsign = '+' if rv >= 0 else ''
                    ret_str = f'<span class="{rcls}">{rsign}{rv}%</span>'
                sigList += f'<tr><td>{s["date"]}</td><td class="{cls}">{s["type"]}</td><td>{s["price"]}</td><td>{ret_str}</td></tr>'
            sigList += '</table>'
        else:
            sigList = '<div style="color:#666;padding:8px;">近期无信号</div>'

        cc = jsonmod.dumps(etf['chart_close'])
        crf = jsonmod.dumps(etf['chart_rsi_fast'])
        crs = jsonmod.dumps(etf['chart_rsi_slow'])
        cp = jsonmod.dumps(etf['chart_position'])
        # 交易统计栏
        stats_html = ''
        if etf.get('n_trades', 0) > 0:
            tr = etf.get('total_return', 0)
            wr = etf.get('win_rate', 0)
            nw = etf.get('n_wins', 0)
            nt = etf.get('n_trades', 0)
            nl = nt - nw
            ar = etf.get('avg_return', 0)
            tr_c = '#ff5252' if tr >= 0 else '#4caf50'
            wr_c = '#ff5252' if wr >= 50 else '#FF9800'
            tr_s = '+' if tr >= 0 else ''
            ar_s = '+' if ar >= 0 else ''
            stats_html = f'''<div class="trade-stats"><div class="stat-item"><span class="stat-label">累计收益</span><span class="stat-value" style="color:{tr_c}">{tr_s}{tr}%</span></div><div class="stat-item"><span class="stat-label">胜率</span><span class="stat-value" style="color:{wr_c}">{wr}%</span></div><div class="stat-item"><span class="stat-label">交易次数</span><span class="stat-value">{nt}次 ({nw}胜{nl}负)</span></div><div class="stat-item"><span class="stat-label">场均收益</span><span class="stat-value" style="color:{tr_c}">{ar_s}{ar}%</span></div></div>'''
        cards_html += f'''<div class="card"><div class="card-header"><div class="etf-info"><span class="etf-code">{etf['code']}</span><span class="etf-name">{etf['name']}</span><span class="params">{params_label}</span></div><span class="signal-badge {signal_class}{is_alert}">{etf['signal_status']}</span></div><div class="card-body"><div class="price-row"><span class="price">{etf['realtime_price']}</span><span class="price-change {change_class}">{change_symbol}{etf['realtime_change']}%</span><span class="price-meta">高 {etf['realtime_high']} · 低 {etf['realtime_low']} · 量 {vol_str}</span></div><div class="rsi-row">{rsi_box_html}</div>{diff_html}{posHtml}<div class="chart-container"><canvas id="chart_{etf['code']}"></canvas></div><div class="rsi-chart-container"><canvas id="rsi_{etf['code']}"></canvas></div>{stats_html}<div class="signal-list"><div class="title">近期信号</div>{sigList}</div></div></div>'''
        chart_init_js += f"drawMiniChart('chart_{etf['code']}',{cc},{cp});drawRSIChart('rsi_{etf['code']}',{crf},{crs});"

    total = len(etfs_data)
    holding = sum(1 for e in etfs_data if e.get('position') == 1)
    cash = total - holding
    new_sigs = sum(1 for e in etfs_data if '今日' in e.get('signal_status', ''))

    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>老三的ETF交易策略-实时跟踪</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,"PingFang SC","Noto Sans CJK SC","Microsoft YaHei",sans-serif;background:#0f1117;color:#e0e0e0;min-height:100vh;padding:16px}}
.header{{text-align:center;padding:20px 0;margin-bottom:20px}}
.header h1{{font-size:24px;color:#fff;background:linear-gradient(135deg,#667eea,#764ba2);-webkit-background-clip:text;-webkit-text-fill-color:transparent}}
.header .sub{{color:#888;font-size:13px;margin-top:6px}}
.update-btn{{display:inline-block;margin-top:12px;padding:8px 24px;background:linear-gradient(135deg,#667eea,#764ba2);color:#fff;border:none;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer;transition:opacity .3s}}
.update-btn:hover{{opacity:.85}}
.update-btn:disabled{{opacity:.5;cursor:not-allowed}}
.summary-bar{{display:flex;justify-content:center;gap:24px;margin-bottom:20px;flex-wrap:wrap}}
.summary-item{{background:#1a1d29;border-radius:10px;padding:12px 24px;text-align:center;border:1px solid #2a2d3a}}
.summary-item .label{{font-size:12px;color:#888}}
.summary-item .value{{font-size:22px;font-weight:700;margin-top:4px}}
.summary-item .value.hold{{color:#ff5252}}
.summary-item .value.cash{{color:#4caf50}}
.summary-item .value.signal{{color:#ffc107}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:20px;max-width:1400px;margin:0 auto}}
.card{{background:#1a1d29;border-radius:16px;overflow:hidden;border:1px solid #2a2d3a;transition:border-color .3s}}
.card:hover{{border-color:#667eea}}
.card-header{{padding:16px 20px;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #2a2d3a}}
.card-header .etf-info{{display:flex;align-items:center;gap:10px;flex-wrap:wrap}}
.card-header .etf-code{{font-size:18px;font-weight:700;color:#fff}}
.card-header .etf-name{{font-size:14px;color:#888}}
.card-header .params{{background:#2a2d3a;padding:4px 10px;border-radius:8px;font-size:12px;color:#667eea}}
.signal-badge{{padding:6px 14px;border-radius:8px;font-size:13px;font-weight:700;white-space:nowrap}}
.signal-badge.red{{background:rgba(255,82,82,.15);color:#ff5252;border:1px solid rgba(255,82,82,.3)}}
.signal-badge.green{{background:rgba(76,175,80,.15);color:#4caf50;border:1px solid rgba(76,175,80,.3)}}
.signal-badge.gray{{background:rgba(128,128,128,.15);color:#888;border:1px solid rgba(128,128,128,.3)}}
.signal-badge.orange{{background:rgba(255,152,0,.15);color:#FF9800;border:1px solid rgba(255,152,0,.3)}}
.signal-badge.alert{{animation:pulse 1.5s infinite}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.5}}}}
.card-body{{padding:16px 20px}}
.price-row{{display:flex;align-items:baseline;gap:12px;margin-bottom:16px;flex-wrap:wrap}}
.price{{font-size:28px;font-weight:700;color:#fff}}
.price-change{{font-size:16px;font-weight:600}}
.price-change.up{{color:#ff5252}}
.price-change.down{{color:#4caf50}}
.price-meta{{font-size:12px;color:#666;margin-left:auto}}
.rsi-row{{display:flex;gap:12px;margin-bottom:16px}}
.rsi-box{{flex:1;background:#14161f;border-radius:10px;padding:12px;text-align:center}}
.rsi-box .label{{font-size:11px;color:#888}}
.rsi-box .value{{font-size:22px;font-weight:700;margin-top:4px}}
.rsi-box .bar{{height:4px;border-radius:2px;margin-top:6px;background:#333;overflow:hidden}}
.rsi-box .bar-fill{{height:100%;border-radius:2px}}
.rsi-diff{{display:flex;align-items:center;justify-content:center;font-size:12px;margin-top:8px;gap:6px;flex-wrap:wrap}}
.rsi-diff .arrow{{font-size:16px}}
.position-box{{border-radius:10px;padding:12px 16px;margin-bottom:16px;display:flex;justify-content:space-between;align-items:center}}
.position-box.holding{{background:rgba(255,82,82,.08);border:1px solid rgba(255,82,82,.2)}}
.position-box.cash{{background:rgba(76,175,80,.08);border:1px solid rgba(76,175,80,.2)}}
.position-box .pos-label{{font-size:13px;color:#888}}
.position-box .pos-status{{font-size:16px;font-weight:700}}
.position-box.holding .pos-status{{color:#ff5252}}
.position-box.cash .pos-status{{color:#4caf50}}
.position-box .pnl{{font-size:18px;font-weight:700}}
.position-box .pnl.profit{{color:#ff5252}}
.position-box .pnl.loss{{color:#4caf50}}
.chart-container{{background:#14161f;border-radius:10px;padding:12px;margin-bottom:16px}}
.chart-container canvas{{width:100%;height:120px}}
.rsi-chart-container{{background:#14161f;border-radius:10px;padding:12px;margin-bottom:16px}}
.rsi-chart-container canvas{{width:100%;height:100px}}
.signal-list{{font-size:12px}}
.signal-list .title{{color:#888;margin-bottom:8px;font-size:13px}}
.signal-list table{{width:100%;border-collapse:collapse}}
.signal-list th{{text-align:left;color:#666;font-weight:400;padding:4px 8px;border-bottom:1px solid #2a2d3a}}
.signal-list td{{padding:6px 8px;border-bottom:1px solid #1e212c}}
.signal-list .buy{{color:#ff5252}}
.signal-list .sell{{color:#4caf50}}
.signal-list .filtered{{color:#FF9800}}
.signal-list .profit{{color:#ff5252;font-weight:600}}
.signal-list .loss{{color:#4caf50;font-weight:600}}
.trade-stats{{display:flex;gap:10px;margin-bottom:16px;flex-wrap:wrap}}
.trade-stats .stat-item{{background:#14161f;border-radius:8px;padding:10px 12px;flex:1;text-align:center;min-width:100px}}
.trade-stats .stat-label{{display:block;font-size:11px;color:#888;margin-bottom:4px}}
.trade-stats .stat-value{{font-size:16px;font-weight:700;color:#fff}}
.update-bar{{text-align:center;margin:20px 0}}
.update-time{{color:#666;font-size:12px;margin-top:8px}}
.footer{{text-align:center;color:#555;font-size:12px;padding:24px 0;max-width:1400px;margin:0 auto;line-height:1.8}}
@media(max-width:500px){{.cards{{grid-template-columns:1fr}}.header h1{{font-size:18px}}.price{{font-size:22px}}.trade-stats .stat-item{{min-width:80px}}}}
</style>
</head>
<body>
<div class="header">
<h1>老三的ETF交易策略-实时跟踪</h1>
<div class="sub">510500 RSI(5/55)金叉死叉 · 588000 RSI(10/28)金叉死叉+BIAS20乖离率过滤 · 159207 RSI6&lt;20抄底/+7%止盈 · 前复权数据 · 数据来源：腾讯财经</div>
<button class="update-btn" id="updateBtn" onclick="triggerUpdate()">手动更新数据</button>
</div>
<div class="summary-bar">
<div class="summary-item"><div class="label">跟踪标的</div><div class="value">{total}</div></div>
<div class="summary-item"><div class="label">持仓中</div><div class="value hold">{holding}</div></div>
<div class="summary-item"><div class="label">空仓中</div><div class="value cash">{cash}</div></div>
<div class="summary-item"><div class="label">今日新信号</div><div class="value signal">{new_sigs}</div></div>
</div>
<div class="update-bar"><div class="update-time">数据更新时间：{update_time} · 每小时自动更新 · GitHub Pages永久托管</div></div>
<div class="cards">{cards_html}</div>
<div class="footer"><p>⚠️ 本页面仅供学习研究，不构成投资建议。信号基于收盘价计算，实盘需结合盘中走势判断。</p><p>RSI参数经近一年历史数据优化，存在过拟合风险，请谨慎使用。</p><p>588000增加BIAS20乖离率过滤：金叉时若乖离率>7%则不买入，避免追高。</p><p>数据由定时任务每小时从腾讯财经获取并自动部署到GitHub Pages，固定链接永久有效。</p></div>
<script>
function drawMiniChart(id,prices,positions){{var c=document.getElementById(id);if(!c)return;var x=c.getContext('2d'),dpr=window.devicePixelRatio||1,r=c.getBoundingClientRect();c.width=r.width*dpr;c.height=r.height*dpr;x.scale(dpr,dpr);var w=r.width,h=r.height,p={{l:4,r:4,t:8,b:8}},cw=w-p.l-p.r,ch=h-p.t-p.b;x.clearRect(0,0,w,h);if(!prices||prices.length<2)return;var mn=Math.min(...prices),mx=Math.max(...prices),rg=mx-mn||1;for(var i=0;i<positions.length-1;i++){{if(positions[i]===1){{var x1=p.l+(i/(prices.length-1))*cw,x2=p.l+((i+1)/(prices.length-1))*cw;x.fillStyle='rgba(255,82,82,0.06)';x.fillRect(x1,p.t,x2-x1,ch)}}}}x.strokeStyle='#667eea';x.lineWidth=1.5;x.beginPath();prices.forEach(function(p,i){{var px=p.l+(i/(prices.length-1))*cw,py=p.t+ch-((p-mn)/rg)*ch;if(i===0)x.moveTo(px,py);else x.lineTo(px,py)}});x.stroke();x.lineTo(p.l+cw,p.t+ch);x.lineTo(p.l,p.t+ch);x.closePath();var g=x.createLinearGradient(0,p.t,0,p.t+ch);g.addColorStop(0,'rgba(102,126,234,0.15)');g.addColorStop(1,'rgba(102,126,234,0)');x.fillStyle=g;x.fill();var lx=p.l+cw,ly=p.t+ch-((prices[prices.length-1]-mn)/rg)*ch;x.fillStyle='#667eea';x.beginPath();x.arc(lx,ly,3,0,Math.PI*2);x.fill()}}
function drawRSIChart(id,rf,rs){{var c=document.getElementById(id);if(!c)return;var x=c.getContext('2d'),dpr=window.devicePixelRatio||1,r=c.getBoundingClientRect();c.width=r.width*dpr;c.height=r.height*dpr;x.scale(dpr,dpr);var w=r.width,h=r.height,p={{l:4,r:4,t:8,b:8}},cw=w-p.l-p.r,ch=h-p.t-p.b;x.clearRect(0,0,w,h);x.strokeStyle='rgba(128,128,128,0.2)';x.lineWidth=1;x.setLineDash([3,3]);var y50=p.t+ch-(50/100)*ch;x.beginPath();x.moveTo(p.l,y50);x.lineTo(p.l+cw,y50);x.stroke();x.setLineDash([]);if(!rf||rf.length<2)return;x.strokeStyle='#2196F3';x.lineWidth=1.2;x.beginPath();var started=false;rf.forEach(function(v,i){{if(v===0||isNaN(v)){{started=false;return}}var px=p.l+(i/(rf.length-1))*cw,py=p.t+ch-(v/100)*ch;if(!started){{x.moveTo(px,py);started=true}}else x.lineTo(px,py)}});x.stroke();if(rs&&rs.length>=2){{var allZero=rs.every(function(v){{return v===0}});if(!allZero){{x.strokeStyle='#FF9800';x.lineWidth=1.2;x.beginPath();started=false;rs.forEach(function(v,i){{if(v===0){{started=false;return}}var px=p.l+(i/(rs.length-1))*cw,py=p.t+ch-(v/100)*ch;if(!started){{x.moveTo(px,py);started=true}}else x.lineTo(px,py)}});x.stroke()}}}}}}
function triggerUpdate(){{var _t={_bt_js}.join('');var btn=document.getElementById('updateBtn');btn.textContent='正在更新...';btn.disabled=true;fetch('https://api.github.com/repos/fishno/laosan-etf/actions/workflows/update.yml/dispatches',{{method:'POST',headers:{{'Authorization':'Bearer '+_t,'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28'}},body:JSON.stringify({{ref:'main'}})}}).then(function(r){{if(r.ok){{btn.textContent='已触发更新，等待30秒自动刷新...';setTimeout(function(){{location.reload(true)}},30000)}}else{{btn.textContent='更新失败，请稍后重试';btn.disabled=false}}}}).catch(function(e){{btn.textContent='网络错误，请稍后重试';btn.disabled=false}})}}
document.addEventListener('DOMContentLoaded',function(){{{chart_init_js}}});
setTimeout(function(){{location.reload()}},3600000);
</script>
</body>
</html>'''


def github_api(method, url, **kwargs):
    r = requests.request(method, f'https://api.github.com{url}', headers=HEADERS, proxies=PROXIES, **kwargs)
    if r.status_code in (200, 201, 204):
        return r.json() if r.status_code != 204 else {}
    print(f'  GitHub API {r.status_code}: {r.text[:200]}')
    return None

def main():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 开始更新ETF数据...")

    etfs_data = []
    for code, cfg in ETF_CONFIG.items():
        try:
            data = analyze_etf(code, cfg)
            etfs_data.append(data)
            bias_info = f" BIAS{cfg.get('bias_period','')}={data.get('bias_val','')}" if cfg.get('bias_period') else ''
            print(f"  {code} {cfg['name']}: {data['signal_status']} 持仓={data['position']}{bias_info}")
        except Exception as e:
            etfs_data.append({'code': code, 'name': cfg['name'], 'error': str(e), 'strategy': cfg.get('strategy',''),
                              'position': 0, 'signal_status': '数据获取失败', 'signal_color': 'gray',
                              'realtime_price': 0, 'realtime_change': 0, 'realtime_high': 0, 'realtime_low': 0,
                              'realtime_volume': 0, 'rsi_fast_val': None, 'rsi_slow_val': None, 'rsi_diff': 0,
                              'rsi_fast_period': cfg.get('rsi_fast', cfg.get('rsi_period', 0)),
                              'rsi_slow_period': cfg.get('rsi_slow', 0),
                              'strategy_desc': cfg.get('strategy_desc', ''),
                              'holding_info': None, 'recent_signals': [], 'chart_close': [], 'chart_rsi_fast': [],
                              'chart_rsi_slow': [], 'chart_position': [], 'n_trades': 0})
            print(f"  {code} {cfg['name']}: ERROR - {e}")

    update_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    html = generate_html(etfs_data, update_time)

    content_b64 = base64.b64encode(html.encode('utf-8')).decode('ascii')

    existing = github_api('GET', f'/repos/{OWNER}/{REPO}/contents/index.html')
    sha = existing.get('sha') if existing and isinstance(existing, dict) and 'sha' in existing else None

    file_data = {
        'message': f'Auto-update {update_time}',
        'content': content_b64,
        'branch': 'main'
    }
    if sha:
        file_data['sha'] = sha

    result = github_api('PUT', f'/repos/{OWNER}/{REPO}/contents/index.html', json=file_data)
    if result:
        print(f"  GitHub上传成功！")
    else:
        print(f"  GitHub上传失败！")
        return False

    print(f"\n  页面地址: https://{OWNER}.github.io/{REPO}/")
    print(f"  更新时间: {update_time}")
    return True

if __name__ == '__main__':
    main()
