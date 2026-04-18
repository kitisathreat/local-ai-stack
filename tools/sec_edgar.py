"""
title: SEC EDGAR — Company Filings & Disclosures
author: local-ai-stack
description: Search SEC EDGAR for public company filings. Look up 10-K annual reports, 10-Q quarterly reports, 8-K current reports, S-1 IPO filings, and proxy statements. Search by company name, ticker, or CIK number. No API key required.
required_open_webui_version: 0.4.0
requirements: httpx
version: 1.0.0
licence: MIT
"""

import httpx
import json
from typing import Callable, Any, Optional
from pydantic import BaseModel, Field

EDGAR_BASE = "https://data.sec.gov"
EFTS_BASE = "https://efts.sec.gov"
SUBMISSIONS_BASE = "https://data.sec.gov/submissions"

FILING_TYPES = {
    "10-K": "Annual Report",
    "10-Q": "Quarterly Report",
    "8-K": "Current Report (Material Events)",
    "S-1": "IPO Registration Statement",
    "DEF 14A": "Proxy Statement",
    "4": "Insider Transaction",
    "13F": "Institutional Holdings (>$100M)",
    "13D": "Activist Investor Disclosure (>5%)",
    "13G": "Passive Investor Disclosure (>5%)",
    "SC TO-T": "Tender Offer",
}


class Tools:
    class Valves(BaseModel):
        USER_EMAIL: str = Field(
            default="user@example.com",
            description="Your email — required in User-Agent by SEC EDGAR policy",
        )

    def __init__(self):
        self.valves = self.Valves()

    def _headers(self):
        return {
            "User-Agent": f"local-ai-stack {self.valves.USER_EMAIL}",
            "Accept": "application/json",
        }

    async def find_company(
        self,
        query: str,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Search for a company on SEC EDGAR by name or ticker symbol. Returns CIK numbers needed for filing lookups.
        :param query: Company name or ticker (e.g. 'Apple', 'MSFT', 'Goldman Sachs', 'NVDA')
        :return: Matching companies with CIK numbers, tickers, and SIC codes
        """
        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Searching EDGAR for '{query}'...", "done": False}})

        try:
            async with httpx.AsyncClient(timeout=15, headers=self._headers()) as client:
                resp = await client.get(
                    "https://efts.sec.gov/LATEST/search-index?q=%22" + query.replace(" ", "%20") + "%22&dateRange=custom&startdt=2020-01-01&forms=10-K",
                )

                # Use the company search API
                search_resp = await client.get(
                    f"https://efts.sec.gov/LATEST/search-index?q={query}&forms=10-K",
                )

                # Better: use the company tickers JSON
                tickers_resp = await client.get(
                    f"{EDGAR_BASE}/files/company_tickers.json",
                )
                tickers_resp.raise_for_status()
                tickers_data = tickers_resp.json()

        except Exception as e:
            return f"EDGAR search error: {str(e)}"

        query_lower = query.lower()
        matches = []
        for _, company in tickers_data.items():
            name = company.get("title", "").lower()
            ticker = company.get("ticker", "").lower()
            cik = company.get("cik_str", "")
            if query_lower in name or query_lower == ticker:
                matches.append({
                    "name": company.get("title", ""),
                    "ticker": company.get("ticker", ""),
                    "cik": str(cik).zfill(10),
                })
            if len(matches) >= 15:
                break

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        if not matches:
            return f"No companies found for '{query}'. Try a broader search term."

        lines = [f"## EDGAR Company Search: '{query}' — {len(matches)} results\n"]
        lines.append("| Company | Ticker | CIK |")
        lines.append("|---------|--------|-----|")
        for m in matches:
            cik_link = f"[{m['cik']}](https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={m['cik']}&type=10-K)"
            lines.append(f"| {m['name']} | {m['ticker']} | {cik_link} |")

        lines.append(f"\nUse the CIK number with `get_filings(cik='...')` to retrieve SEC filings.")
        return "\n".join(lines)

    async def get_filings(
        self,
        cik: str = "",
        ticker: str = "",
        filing_type: str = "10-K",
        limit: int = 10,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Get recent SEC filings for a company by CIK number or ticker symbol.
        :param cik: Company CIK number (10 digits, from find_company) — use either cik or ticker
        :param ticker: Stock ticker symbol as alternative to CIK (e.g. 'AAPL', 'MSFT')
        :param filing_type: Filing type (10-K, 10-Q, 8-K, S-1, DEF 14A, 4, 13F) — default 10-K
        :param limit: Number of filings to return (max 20)
        :return: List of filings with dates and direct links to SEC documents
        """
        if not cik and not ticker:
            return "Provide either a CIK number or ticker symbol."

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Fetching {filing_type} filings from EDGAR...", "done": False}})

        # Resolve ticker to CIK if needed
        if ticker and not cik:
            try:
                async with httpx.AsyncClient(timeout=15, headers=self._headers()) as client:
                    resp = await client.get(f"{EDGAR_BASE}/files/company_tickers.json")
                    resp.raise_for_status()
                    data = resp.json()
                ticker_upper = ticker.upper()
                for _, company in data.items():
                    if company.get("ticker", "").upper() == ticker_upper:
                        cik = str(company.get("cik_str", "")).zfill(10)
                        break
                if not cik:
                    return f"Ticker '{ticker}' not found in EDGAR. Try `find_company('{ticker}')` first."
            except Exception as e:
                return f"Ticker lookup error: {str(e)}"

        cik = cik.lstrip("0").zfill(10)

        try:
            async with httpx.AsyncClient(timeout=15, headers=self._headers()) as client:
                resp = await client.get(f"{SUBMISSIONS_BASE}/CIK{cik}.json")
                if resp.status_code == 404:
                    return f"CIK '{cik}' not found. Use find_company() to get the correct CIK."
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            return f"EDGAR filings error: {str(e)}"

        company_name = data.get("name", "Unknown")
        filings = data.get("filings", {}).get("recent", {})

        forms = filings.get("form", [])
        dates = filings.get("filingDate", [])
        accessions = filings.get("accessionNumber", [])
        descriptions = filings.get("primaryDocument", [])
        report_dates = filings.get("reportDate", [])

        # Filter by filing type
        filtered = []
        for i, form in enumerate(forms):
            if filing_type.upper() in form.upper() or form.upper() == filing_type.upper():
                filtered.append({
                    "form": form,
                    "date": dates[i] if i < len(dates) else "",
                    "report_date": report_dates[i] if i < len(report_dates) else "",
                    "accession": accessions[i] if i < len(accessions) else "",
                    "doc": descriptions[i] if i < len(descriptions) else "",
                })
            if len(filtered) >= min(limit, 20):
                break

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        type_desc = FILING_TYPES.get(filing_type.upper(), filing_type)
        lines = [f"## {company_name} — {filing_type} Filings ({type_desc})\n"]
        lines.append(f"CIK: {cik} | [EDGAR Profile](https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type={filing_type})\n")

        if not filtered:
            lines.append(f"No {filing_type} filings found.")
            return "\n".join(lines)

        lines.append("| Form | Filed | Period | View Filing |")
        lines.append("|------|-------|--------|-------------|")
        for f in filtered:
            acc = f["accession"].replace("-", "")
            acc_formatted = f["accession"]
            viewer_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{f['doc']}"
            index_url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type={filing_type}&dateb=&owner=include&count=40"
            lines.append(f"| {f['form']} | {f['date']} | {f['report_date']} | [View]({viewer_url}) |")

        return "\n".join(lines)

    async def search_filings_text(
        self,
        query: str,
        filing_type: str = "10-K",
        date_from: str = "",
        limit: int = 10,
        __event_emitter__: Callable[[dict], Any] = None,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Full-text search across all SEC filings for specific terms (uses EDGAR EFTS).
        :param query: Search terms (e.g. 'artificial intelligence risk', 'China exposure', 'going concern', 'cybersecurity incident')
        :param filing_type: Filter by form type (10-K, 10-Q, 8-K, S-1, or leave blank for all)
        :param date_from: Start date filter (YYYY-MM-DD, e.g. '2023-01-01')
        :param limit: Max results (up to 20)
        :return: Matching filings with company, date, and link
        """
        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": f"Searching EDGAR full text for '{query}'...", "done": False}})

        params = {
            "q": f'"{query}"',
            "dateRange": "custom" if date_from else "",
            "startdt": date_from or "",
            "forms": filing_type,
            "_source": "file_date,period_of_report,entity_name,file_num,form_type",
            "hits.hits.total.value": "true",
            "hits.hits._source.period_of_report": "true",
        }
        params = {k: v for k, v in params.items() if v}

        try:
            async with httpx.AsyncClient(timeout=20, headers=self._headers()) as client:
                resp = await client.get(
                    f"{EFTS_BASE}/LATEST/search-index",
                    params={"q": query, "forms": filing_type, "dateRange": "custom" if date_from else "", "startdt": date_from or ""},
                )
                # Use the EDGAR full-text search
                search_resp = await client.get(
                    "https://efts.sec.gov/LATEST/search-index",
                    params={
                        "q": f'"{query}"',
                        "forms": filing_type,
                        **({"dateRange": "custom", "startdt": date_from} if date_from else {}),
                    },
                )

        except Exception as e:
            return f"EDGAR text search error: {str(e)}"

        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Done", "done": True}})

        # Use the EDGAR EFTS search endpoint
        try:
            async with httpx.AsyncClient(timeout=20, headers=self._headers()) as client:
                params = {"q": f'"{query}"'}
                if filing_type:
                    params["forms"] = filing_type
                if date_from:
                    params["dateRange"] = "custom"
                    params["startdt"] = date_from
                resp = await client.get("https://efts.sec.gov/LATEST/search-index", params=params)
                data = resp.json()
        except Exception as e:
            return f"EDGAR full-text search error: {str(e)}"

        hits = data.get("hits", {}).get("hits", [])
        total = data.get("hits", {}).get("total", {}).get("value", 0)

        if not hits:
            return (
                f"No EDGAR filings found containing '{query}'.\n"
                f"Try the EDGAR full-text search directly: https://efts.sec.gov/LATEST/search-index?q=%22{query.replace(' ', '%20')}%22"
            )

        lines = [f"## EDGAR Full-Text Search: '{query}'\n"]
        lines.append(f"Found {total:,} filings containing this phrase.\n")
        lines.append("| Company | Form | Filed | Link |")
        lines.append("|---------|------|-------|------|")

        for hit in hits[:min(limit, 20)]:
            src = hit.get("_source", {})
            entity = src.get("entity_name", "Unknown")
            form = src.get("form_type", "")
            filed = src.get("file_date", "")
            accession = src.get("accession_no", "").replace("-", "")
            url = f"https://www.sec.gov/Archives/edgar/data/{src.get('ciks', [''])[0]}/{accession}/" if accession else "#"
            lines.append(f"| {entity} | {form} | {filed} | [View]({url}) |")

        return "\n".join(lines)

    def list_filing_types(self, __user__: Optional[dict] = None) -> str:
        """
        List all supported SEC filing types with descriptions.
        :return: Table of form types and what they contain
        """
        lines = ["## SEC Filing Types\n"]
        lines.append("| Form | Description |")
        lines.append("|------|-------------|")
        for form, desc in FILING_TYPES.items():
            lines.append(f"| **{form}** | {desc} |")
        lines.append("\nUse any form type with `get_filings(filing_type='...')`.")
        return "\n".join(lines)
