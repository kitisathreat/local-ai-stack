"""
title: Financial Calculator — NPV, IRR, DCF, Options & Portfolio
author: local-ai-stack
description: Professional financial calculations including Net Present Value (NPV), Internal Rate of Return (IRR), Discounted Cash Flow (DCF) valuation, loan amortization schedules, bond yield-to-maturity, Black-Scholes options pricing, and portfolio performance metrics (Sharpe ratio, Sortino ratio, maximum drawdown). No API key required.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import math
import json
from typing import Callable, Any, Optional, List
from pydantic import BaseModel, Field


class Tools:
    class Valves(BaseModel):
        pass

    def __init__(self):
        self.valves = self.Valves()

    def calculate_npv_irr(
        self,
        cash_flows: str,
        discount_rate: float = 10.0,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Calculate Net Present Value (NPV) and Internal Rate of Return (IRR) for a series of cash flows.
        :param cash_flows: JSON array of cash flows where index 0 is the initial investment (negative). E.g. '[-100000, 25000, 30000, 35000, 40000]'
        :param discount_rate: Annual discount/hurdle rate in percent (e.g. 10 for 10%)
        :return: NPV, IRR, payback period, and profitability index
        """
        try:
            flows = json.loads(cash_flows)
            if not isinstance(flows, list) or len(flows) < 2:
                return "cash_flows must be a JSON array with at least 2 values (e.g. [-100000, 30000, 40000])"
        except Exception:
            return f"Invalid cash_flows JSON. Example: '[-100000, 25000, 30000, 35000, 40000]'"

        rate = discount_rate / 100

        # NPV
        npv = sum(cf / (1 + rate) ** t for t, cf in enumerate(flows))

        # IRR via Newton-Raphson
        irr = self._calculate_irr(flows)

        # Payback period
        cumulative = 0
        payback = None
        for t, cf in enumerate(flows):
            cumulative += cf
            if cumulative >= 0:
                prev = cumulative - cf
                payback = t - 1 + abs(prev) / abs(cf) if cf != 0 else t
                break

        # Profitability Index
        initial = abs(flows[0]) if flows[0] < 0 else 1
        pv_future = npv + abs(flows[0]) if flows[0] < 0 else npv
        pi = pv_future / initial if initial > 0 else None

        lines = ["## NPV & IRR Analysis\n"]
        lines.append(f"**Discount Rate:** {discount_rate}%")
        lines.append(f"**Cash Flows:** {flows}\n")

        lines.append("| Metric | Value |")
        lines.append("|--------|-------|")
        lines.append(f"| **NPV** | ${npv:,.2f} |")
        lines.append(f"| **IRR** | {irr*100:.2f}% |" if irr is not None else "| **IRR** | Not calculable |")
        if payback is not None:
            lines.append(f"| **Payback Period** | {payback:.2f} years |")
        if pi is not None:
            lines.append(f"| **Profitability Index** | {pi:.3f} |")

        lines.append("\n**Interpretation:**")
        if npv > 0:
            lines.append(f"- NPV > 0 → Project creates ${npv:,.0f} of value → **ACCEPT**")
        elif npv < 0:
            lines.append(f"- NPV < 0 → Project destroys ${abs(npv):,.0f} of value → **REJECT**")
        else:
            lines.append("- NPV = 0 → Project breaks even at the discount rate")

        if irr is not None:
            if irr * 100 > discount_rate:
                lines.append(f"- IRR ({irr*100:.2f}%) > Hurdle Rate ({discount_rate}%) → **Accept** by IRR criterion")
            else:
                lines.append(f"- IRR ({irr*100:.2f}%) < Hurdle Rate ({discount_rate}%) → **Reject** by IRR criterion")

        # Period-by-period table
        lines.append("\n### Cash Flow Schedule\n")
        lines.append("| Year | Cash Flow | PV of Cash Flow | Cumulative |")
        lines.append("|------|-----------|-----------------|-----------|")
        cum = 0
        for t, cf in enumerate(flows):
            pv = cf / (1 + rate) ** t
            cum += cf
            lines.append(f"| {t} | ${cf:,.0f} | ${pv:,.2f} | ${cum:,.0f} |")

        return "\n".join(lines)

    def _calculate_irr(self, flows: list, guess: float = 0.1) -> Optional[float]:
        rate = guess
        for _ in range(1000):
            try:
                f = sum(cf / (1 + rate) ** t for t, cf in enumerate(flows))
                df = sum(-t * cf / (1 + rate) ** (t + 1) for t, cf in enumerate(flows))
                if abs(df) < 1e-12:
                    break
                new_rate = rate - f / df
                if abs(new_rate - rate) < 1e-8:
                    return new_rate
                rate = new_rate
            except (OverflowError, ZeroDivisionError):
                break
        return None

    def loan_amortization(
        self,
        principal: float,
        annual_rate: float,
        years: int,
        extra_payment: float = 0,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Calculate loan amortization schedule with monthly payments, interest vs principal breakdown.
        :param principal: Loan amount in dollars (e.g. 300000 for a $300k mortgage)
        :param annual_rate: Annual interest rate in percent (e.g. 6.5 for 6.5%)
        :param years: Loan term in years (e.g. 30)
        :param extra_payment: Optional extra monthly payment toward principal (reduces payoff time)
        :return: Payment schedule summary with total interest paid and payoff date
        """
        monthly_rate = annual_rate / 100 / 12
        n_payments = years * 12

        if monthly_rate == 0:
            monthly_payment = principal / n_payments
        else:
            monthly_payment = principal * (monthly_rate * (1 + monthly_rate) ** n_payments) / ((1 + monthly_rate) ** n_payments - 1)

        total_monthly = monthly_payment + extra_payment

        lines = ["## Loan Amortization Schedule\n"]
        lines.append(f"**Principal:** ${principal:,.2f}")
        lines.append(f"**Annual Rate:** {annual_rate}%")
        lines.append(f"**Term:** {years} years ({n_payments} payments)")
        lines.append(f"**Base Monthly Payment:** ${monthly_payment:,.2f}")
        if extra_payment > 0:
            lines.append(f"**Extra Monthly Payment:** ${extra_payment:,.2f}")
            lines.append(f"**Total Monthly:** ${total_monthly:,.2f}\n")

        # Run amortization
        balance = principal
        total_interest = 0
        total_paid = 0
        month = 0
        schedule_rows = []

        while balance > 0.01 and month < n_payments * 2:
            month += 1
            interest = balance * monthly_rate
            principal_pmt = min(total_monthly - interest, balance)
            balance = max(0, balance - principal_pmt)
            total_interest += interest
            total_paid += interest + principal_pmt

            if month <= 24 or month % 12 == 0 or balance < 0.01:
                schedule_rows.append((month, interest + principal_pmt, interest, principal_pmt, balance))

        lines.append("| Month | Payment | Interest | Principal | Balance |")
        lines.append("|-------|---------|----------|-----------|---------|")
        for row in schedule_rows:
            m, pmt, intr, princ, bal = row
            lines.append(f"| {m} | ${pmt:,.2f} | ${intr:,.2f} | ${princ:,.2f} | ${bal:,.2f} |")

        lines.append(f"\n### Summary")
        lines.append(f"- **Total Paid:** ${total_paid:,.2f}")
        lines.append(f"- **Total Interest:** ${total_interest:,.2f}")
        lines.append(f"- **Payoff:** {month} months ({month/12:.1f} years)")
        if extra_payment > 0:
            standard_months = n_payments
            saved_months = standard_months - month
            lines.append(f"- **Time Saved:** {saved_months} months ({saved_months/12:.1f} years) vs no extra payment")
            standard_interest = monthly_payment * n_payments - principal
            lines.append(f"- **Interest Saved:** ${standard_interest - total_interest:,.2f}")

        return "\n".join(lines)

    def black_scholes(
        self,
        spot: float,
        strike: float,
        time_to_expiry: float,
        volatility: float,
        risk_free_rate: float = 5.0,
        option_type: str = "call",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Price a European options contract using the Black-Scholes model. Also calculates option Greeks.
        :param spot: Current stock price (e.g. 150.0)
        :param strike: Option strike price (e.g. 155.0)
        :param time_to_expiry: Time to expiration in years (e.g. 0.25 for 3 months, 0.0833 for 1 month)
        :param volatility: Implied volatility in percent (e.g. 30 for 30% IV)
        :param risk_free_rate: Risk-free rate in percent (e.g. 5 for 5%)
        :param option_type: 'call' or 'put'
        :return: Option price and Greeks (delta, gamma, theta, vega, rho)
        """
        S = spot
        K = strike
        T = time_to_expiry
        sigma = volatility / 100
        r = risk_free_rate / 100

        if T <= 0:
            return "Time to expiry must be positive."
        if sigma <= 0:
            return "Volatility must be positive."

        def norm_cdf(x):
            return (1 + math.erf(x / math.sqrt(2))) / 2

        def norm_pdf(x):
            return math.exp(-0.5 * x ** 2) / math.sqrt(2 * math.pi)

        d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)

        if option_type.lower() == "call":
            price = S * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)
            delta = norm_cdf(d1)
            rho = K * T * math.exp(-r * T) * norm_cdf(d2) / 100
        else:
            price = K * math.exp(-r * T) * norm_cdf(-d2) - S * norm_cdf(-d1)
            delta = norm_cdf(d1) - 1
            rho = -K * T * math.exp(-r * T) * norm_cdf(-d2) / 100

        gamma = norm_pdf(d1) / (S * sigma * math.sqrt(T))
        theta = (-(S * norm_pdf(d1) * sigma) / (2 * math.sqrt(T)) - r * K * math.exp(-r * T) * norm_cdf(d2 if option_type.lower() == "call" else -d2)) / 365
        vega = S * norm_pdf(d1) * math.sqrt(T) / 100

        intrinsic = max(0, S - K) if option_type.lower() == "call" else max(0, K - S)
        time_value = price - intrinsic
        moneyness = "ATM" if abs(S - K) / K < 0.02 else ("ITM" if intrinsic > 0 else "OTM")

        lines = [f"## Black-Scholes: {option_type.upper()} Option\n"]
        lines.append(f"**Inputs:** S=${S}, K=${K}, T={T:.4f}y, σ={volatility}%, r={risk_free_rate}%\n")
        lines.append(f"**Moneyness:** {moneyness} | d1={d1:.4f}, d2={d2:.4f}\n")

        lines.append("| Metric | Value |")
        lines.append("|--------|-------|")
        lines.append(f"| **Option Price** | **${price:.4f}** |")
        lines.append(f"| Intrinsic Value | ${intrinsic:.4f} |")
        lines.append(f"| Time Value | ${time_value:.4f} |")
        lines.append(f"| **Delta (Δ)** | {delta:.4f} |")
        lines.append(f"| **Gamma (Γ)** | {gamma:.6f} |")
        lines.append(f"| **Theta (Θ)** | ${theta:.4f}/day |")
        lines.append(f"| **Vega (ν)** | ${vega:.4f}/1% vol |")
        lines.append(f"| **Rho (ρ)** | ${rho:.4f}/1% rate |")

        lines.append("\n**Greeks Interpretation:**")
        lines.append(f"- Delta {delta:.2f}: Option moves ${abs(delta):.2f} for each $1 move in the stock")
        lines.append(f"- Theta {theta:.4f}: Option loses ${abs(theta):.4f} per day from time decay")
        lines.append(f"- Vega {vega:.4f}: Option gains ${vega:.4f} for each 1% increase in volatility")

        return "\n".join(lines)

    def portfolio_metrics(
        self,
        returns: str,
        risk_free_rate: float = 5.0,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Calculate portfolio performance metrics: Sharpe ratio, Sortino ratio, max drawdown, CAGR, and volatility.
        :param returns: JSON array of periodic returns in percent (e.g. '[2.1, -1.3, 3.4, 0.8, -2.1, 1.5]' for monthly returns)
        :param risk_free_rate: Annual risk-free rate in percent (e.g. 5 for 5%)
        :return: Sharpe ratio, Sortino ratio, max drawdown, CAGR, and annualized volatility
        """
        try:
            rets = json.loads(returns)
            if not isinstance(rets, list) or len(rets) < 3:
                return "returns must be a JSON array of at least 3 values (e.g. '[2.1, -1.3, 3.4]')"
            rets = [r / 100 for r in rets]
        except Exception:
            return "Invalid returns JSON. Example: '[2.1, -1.3, 3.4, 0.8, -2.1, 1.5]'"

        n = len(rets)
        rf_period = (1 + risk_free_rate / 100) ** (1 / 12) - 1  # assume monthly

        mean_ret = sum(rets) / n
        variance = sum((r - mean_ret) ** 2 for r in rets) / (n - 1)
        std_dev = math.sqrt(variance)

        # Annualized (assuming monthly returns)
        ann_return = (1 + mean_ret) ** 12 - 1
        ann_vol = std_dev * math.sqrt(12)

        # Sharpe
        excess = [r - rf_period for r in rets]
        mean_excess = sum(excess) / n
        sharpe = (mean_excess / std_dev) * math.sqrt(12) if std_dev > 0 else 0

        # Sortino (downside deviation)
        downside = [min(0, r - rf_period) for r in rets]
        downside_var = sum(d ** 2 for d in downside) / n
        downside_std = math.sqrt(downside_var)
        sortino = (mean_excess / downside_std) * math.sqrt(12) if downside_std > 0 else float('inf')

        # Max Drawdown
        cumulative = 1.0
        peak = 1.0
        max_dd = 0.0
        values = []
        for r in rets:
            cumulative *= (1 + r)
            values.append(cumulative)
            if cumulative > peak:
                peak = cumulative
            dd = (peak - cumulative) / peak
            if dd > max_dd:
                max_dd = dd

        # CAGR
        total_return = values[-1] - 1
        cagr = (1 + total_return) ** (12 / n) - 1

        # Calmar ratio
        calmar = cagr / max_dd if max_dd > 0 else float('inf')

        # Win rate
        wins = sum(1 for r in rets if r > 0)
        win_rate = wins / n * 100

        lines = ["## Portfolio Performance Metrics\n"]
        lines.append(f"**Periods:** {n} months | **Risk-Free Rate:** {risk_free_rate}% annual\n")

        lines.append("| Metric | Value | Interpretation |")
        lines.append("|--------|-------|----------------|")
        lines.append(f"| **CAGR** | {cagr*100:.2f}% | Annualized return |")
        lines.append(f"| **Total Return** | {total_return*100:.2f}% | Over {n} months |")
        lines.append(f"| **Ann. Volatility** | {ann_vol*100:.2f}% | Annualized std dev |")
        lines.append(f"| **Sharpe Ratio** | {sharpe:.3f} | {'>1: Good, >2: Great, >3: Excellent' if sharpe > 0 else 'Negative: Underperforms risk-free'} |")
        lines.append(f"| **Sortino Ratio** | {sortino:.3f} | Downside-adjusted return |")
        lines.append(f"| **Max Drawdown** | -{max_dd*100:.2f}% | Worst peak-to-trough loss |")
        lines.append(f"| **Calmar Ratio** | {calmar:.3f} | CAGR / Max Drawdown |")
        lines.append(f"| **Win Rate** | {win_rate:.1f}% | Fraction of positive periods |")
        lines.append(f"| **Best Period** | {max(rets)*100:.2f}% | |")
        lines.append(f"| **Worst Period** | {min(rets)*100:.2f}% | |")

        return "\n".join(lines)

    def bond_yield_to_maturity(
        self,
        face_value: float,
        coupon_rate: float,
        years_to_maturity: float,
        current_price: float,
        frequency: int = 2,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Calculate bond yield-to-maturity (YTM), duration, and convexity.
        :param face_value: Bond face/par value (e.g. 1000)
        :param coupon_rate: Annual coupon rate in percent (e.g. 5 for a 5% coupon)
        :param years_to_maturity: Years until the bond matures (e.g. 10)
        :param current_price: Current market price of the bond (e.g. 950)
        :param frequency: Coupon payments per year (1=annual, 2=semi-annual, 4=quarterly)
        :return: YTM, current yield, Macaulay duration, modified duration, and DV01
        """
        FV = face_value
        c = coupon_rate / 100
        n = int(years_to_maturity * frequency)
        P = current_price
        coupon = FV * c / frequency

        # Current yield
        current_yield = coupon * frequency / P

        # YTM via Newton-Raphson
        ytm_period = c / frequency  # initial guess
        for _ in range(1000):
            price_est = sum(coupon / (1 + ytm_period) ** t for t in range(1, n + 1)) + FV / (1 + ytm_period) ** n
            dprice = sum(-t * coupon / (1 + ytm_period) ** (t + 1) for t in range(1, n + 1)) - n * FV / (1 + ytm_period) ** (n + 1)
            delta = (price_est - P) / dprice
            ytm_period -= delta
            if abs(delta) < 1e-10:
                break

        ytm_annual = ytm_period * frequency

        # Macaulay Duration
        weights = []
        for t in range(1, n + 1):
            cf = coupon if t < n else coupon + FV
            pv_cf = cf / (1 + ytm_period) ** t
            weights.append((t / frequency) * pv_cf)
        mac_duration = sum(weights) / P

        # Modified Duration
        mod_duration = mac_duration / (1 + ytm_period)

        # DV01 (dollar value of a basis point)
        dv01 = mod_duration * P * 0.0001

        lines = ["## Bond Analysis\n"]
        lines.append(f"**Face Value:** ${FV:,.2f} | **Coupon:** {coupon_rate}% | **Maturity:** {years_to_maturity}y | **Price:** ${P:,.2f}\n")

        premium_discount = "Premium" if P > FV else ("Discount" if P < FV else "Par")
        lines.append(f"**Trading at:** {premium_discount} (${P - FV:+,.2f} vs par)\n")

        lines.append("| Metric | Value |")
        lines.append("|--------|-------|")
        lines.append(f"| **YTM (Annual)** | **{ytm_annual*100:.4f}%** |")
        lines.append(f"| Coupon Payment | ${coupon:,.2f} × {frequency}/year = ${coupon*frequency:,.2f}/year |")
        lines.append(f"| Current Yield | {current_yield*100:.4f}% |")
        lines.append(f"| Macaulay Duration | {mac_duration:.4f} years |")
        lines.append(f"| Modified Duration | {mod_duration:.4f} |")
        lines.append(f"| DV01 | ${dv01:,.4f} per basis point |")

        # Price sensitivity
        lines.append(f"\n**Price Sensitivity (Modified Duration {mod_duration:.2f}):**")
        for rate_change in [-100, -50, -25, 25, 50, 100]:
            price_change = -mod_duration * P * rate_change / 10000
            new_price = P + price_change
            lines.append(f"- Rates {rate_change:+}bp → Price: ${new_price:,.2f} ({price_change:+.2f})")

        return "\n".join(lines)

    def compound_interest_calculator(
        self,
        principal: float,
        annual_rate: float,
        years: float,
        monthly_contribution: float = 0,
        compounding: str = "monthly",
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Calculate compound interest growth with optional regular contributions. Shows year-by-year breakdown.
        :param principal: Initial investment amount
        :param annual_rate: Annual interest/return rate in percent (e.g. 7 for 7%)
        :param years: Investment time horizon in years
        :param monthly_contribution: Optional monthly contribution added each period
        :param compounding: Compounding frequency: 'daily', 'monthly', 'quarterly', 'annually'
        :return: Final value, total interest earned, and year-by-year growth table
        """
        freq_map = {"daily": 365, "monthly": 12, "quarterly": 4, "annually": 1}
        n = freq_map.get(compounding.lower(), 12)
        r = annual_rate / 100 / n
        contrib_per_period = monthly_contribution * (12 / n)

        lines = ["## Compound Interest Calculator\n"]
        lines.append(f"**Principal:** ${principal:,.2f} | **Rate:** {annual_rate}% | **Years:** {years} | **Compounding:** {compounding}")
        if monthly_contribution > 0:
            lines.append(f"**Monthly Contribution:** ${monthly_contribution:,.2f}")
        lines.append("")

        # Year-by-year
        lines.append("| Year | Balance | Interest Earned | Total Contributed |")
        lines.append("|------|---------|----------------|-------------------|")

        balance = principal
        total_contrib = principal
        prev_balance = principal

        for year in range(1, int(years) + 1):
            for _ in range(n):
                balance = balance * (1 + r) + contrib_per_period
                total_contrib += contrib_per_period * (12 / n)
            interest_this_year = balance - prev_balance - (monthly_contribution * 12)
            prev_balance = balance
            lines.append(f"| {year} | ${balance:,.2f} | ${balance - total_contrib:,.2f} | ${total_contrib:,.2f} |")

        total_interest = balance - total_contrib
        lines.append(f"\n**Final Balance:** ${balance:,.2f}")
        lines.append(f"**Total Contributed:** ${total_contrib:,.2f}")
        lines.append(f"**Total Interest Earned:** ${total_interest:,.2f}")
        lines.append(f"**Effective CAGR:** {((balance/principal)**(1/years)-1)*100:.2f}%")
        if monthly_contribution > 0:
            roi = total_interest / total_contrib * 100
            lines.append(f"**Return on Invested Capital:** {roi:.1f}%")

        return "\n".join(lines)
