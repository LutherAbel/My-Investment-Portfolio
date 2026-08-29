r"""Assemble the single-entry tabbed site from the two data JSONs.

Output: Trades\index.html (also the GitHub Pages entry point).
Run after compute_twr.py and compute_tw.py.
"""
import json

DATA = r"C:\Users\Tr7\Trades\Data"
SCRIPTS = r"C:\Users\Tr7\Trades\scripts"
OUT = r"C:\Users\Tr7\Trades\index.html"

tpl = open(rf"{SCRIPTS}\site_template.html", encoding="utf-8").read()
us = open(rf"{DATA}\twr_data.json", encoding="utf-8").read()
tw = open(rf"{DATA}\tw_data.json", encoding="utf-8").read()
html = tpl.replace("__DATA_US__", us).replace("__DATA_TW__", tw)
open(OUT, "w", encoding="utf-8").write(html)
print("written", OUT, len(html), "bytes")
