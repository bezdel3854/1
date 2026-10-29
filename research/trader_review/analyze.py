"""Offline arithmetic audit of public trader observations; no trading or network."""
import json
from decimal import Decimal
from pathlib import Path


def analyze(observation):
    wins = observation['displayed_wins']
    losses = observation['displayed_losses']
    total = observation['displayed_positions']
    if wins + losses != total:
        raise ValueError('Displayed counts do not reconcile')
    profit = Decimal(str(observation['average_profit_usdt']))
    loss = Decimal(str(observation['average_loss_usdt']))
    if profit <= 0 or loss <= 0:
        raise ValueError('Average profit/loss must be positive magnitudes')
    return {
        'displayed_counts_win_pct': float(100 * Decimal(wins) / total),
        'average_loss_to_average_win': float(loss / profit),
        'conditional_same_period_profit_factor': float(wins * profit / (losses * loss)),
        'conditional_same_period_breakeven_win_pct': float(100 * loss / (profit + loss)),
        'conditional_figures_are_not_validated_performance': True,
        'declared_risk_cap': observation['declared_risk_cap'],
        'actual_position_risk_verified': False,
        'strategy_reproducible': False,
        'automated_replication_ready': False,
        'limitations': [
            'Averages are displayed source values, not recomputed from complete fills.',
            'Displayed 30D win rate is 100%, but 802/901 is 89.01%; scope is unresolved.',
            'Counts and averages must share a period before conditional figures can be interpreted.',
            'Historical equity, entry-time stops and order/position linkage are unavailable.',
            'Current equity cannot establish the risk of an earlier position.',
            'A closed-order row is not necessarily a complete position or strategy decision.',
            'High recent win rate does not establish the long-term loss distribution.',
            'Indicator names and entry/exit thresholds have not been disclosed.',
        ],
    }


if __name__ == '__main__':
    root = Path(__file__).resolve().parents[2]
    folder = root / 'reports' / 'trader-review'
    observation = json.loads((folder / 'harmonic-observation.json').read_text())
    result = analyze(observation)
    (folder / 'arithmetic-audit.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
