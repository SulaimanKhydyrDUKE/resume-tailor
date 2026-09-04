"""The apply half: a persistent browser that can read a form, fill it, attach a
file, and submit.

Why this owns its own browser rather than talking to invisible-playwright-mcp:
that server exposes fourteen tools and none of them reach set_input_files.
Browsers deliberately refuse synthetic events on a file input, so no amount of
click/type/evaluate can attach a PDF. The upload has to happen in the process
that holds the Playwright handle, which means the process that holds the browser.
"""
from __future__ import annotations

import asyncio
import random
import json
import re
import time
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

DEFAULT_PROFILE_DIR = Path.home() / ".resume-tailor" / "browser-profile"

# Pulled from the page rather than guessed at, because every ATS names its
# fields differently and the label is the only thing that reliably describes one.
#
# Three things learned from real Ashby and Greenhouse forms shape this scan:
#  - "required" is rarely on the control. Ashby marks the question label with a
#    `_required_` class and draws the asterisk with CSS; Greenhouse puts a
#    literal "*" in the label text; Lever uses "✱" in a span. The question's
#    container is checked for all of these.
#  - react-select keeps a zero-opacity, tabindex=-1 twin input per dropdown
#    to carry browser validation. It reads as a field and vanishes the moment
#    the real dropdown gets a value — so it is dropped from the scan.
#  - Ashby's Yes/No question is a display:none checkbox under two buttons.
#    Neither is a fillable input, so the pair is reported as one `yesno` field.
# Every page-level query in the scanner scripts also walks open shadow roots:
# SmartRecruiters renders its whole application as web components, and
# document.querySelectorAll never sees inside them (Playwright's locators do,
# so the selectors the scanner hands back still resolve).
_DEEP_PRELUDE = """
  const __all = (() => { const acc = []; const walk = r => { for (const el of r.querySelectorAll('*')) { acc.push(el); if (el.shadowRoot) walk(el.shadowRoot); } }; walk(document); return acc; })();
  const __q = sel => __all.filter(el => el.matches(sel));
  const __id = id => __all.find(el => el.id === id) || null;
"""


def _deep(js: str) -> str:
    """Wrap a `() => …` page script so its document-wide queries pierce shadow DOM."""
    body = js.replace("document.querySelectorAll(", "__q(").replace("document.getElementById(", "__id(")
    return "() => {" + _DEEP_PRELUDE + "  return (" + body.strip() + ")(); }"


_COUNT_JS = _deep("() => document.querySelectorAll('input:not([type=hidden]), select, textarea').length")


_FIELD_JS = r"""
() => {
  const vis = el => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    // A file input is nearly always hidden behind a styled "Upload" button
    // (display:none on Epic's Greenhouse embed); it still takes a file.
    if (el.type === 'file') return true;
    return s.display !== 'none' && s.visibility !== 'hidden' && (r.width > 0 || r.height > 0);
  };
  // Word joiners and zero-width spaces (Epic writes "First Name\u2060*\u2060:") would hide the asterisk.
  const txt = el => (el ? (el.innerText || el.textContent || '') : '').replace(/[\u2060\u200b\ufeff]/g, '').replace(/\s+/g, ' ').trim();
  const isOption = el => ['radio', 'checkbox'].includes((el.type || '').toLowerCase());
  const byIds = ids => ids.split(/\s+/).map(id => document.getElementById(id)).filter(Boolean).map(txt).join(' ').trim();
  const dummy = el => {
    if (isOption(el) || el.type === 'file') return false;
    const s = getComputedStyle(el); const r = el.getBoundingClientRect();
    return (s.opacity === '0' || r.height === 0) && (el.tabIndex === -1 || el.getAttribute('aria-hidden') === 'true');
  };
  // What a control's own label says — for a radio button that is "Yes", not the question.
  const ownLabel = el => {
    if (el.labels && el.labels.length) return txt(el.labels[0]);
    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label').trim();
    const lb = el.getAttribute('aria-labelledby'); if (lb) { const t = byIds(lb); if (t) return t; }
    const wrap = el.closest('label'); if (wrap) return txt(wrap);
    return '';
  };
  // Text that names an action or a hint, not the question: a file input's
  // "Attach" button label, a picker's "Start typing...".
  const placeholderish = t => /^(type your response|your answer|type here.*|enter .*|select\.{0,3}|choose.*|start typing.*|attach( file)?|upload( file)?|browse|choose file|select file|drag and drop.*|drop files?.*)$/i.test(t || '');
  const MAXQ = 600;
  const STAR = /[*✱]\s*:?\s*$/;
  // The block holding one question and its control(s): Ashby's field entry,
  // Lever's application-question, a generic field wrapper.
  const entryOf = el => el.closest('[data-field-path], [class*="field-entry"], [class*="application-question"], [class*="form-field"], [class*="field-wrapper"], [class*="field-container"]');
  const titleOf = box => box && box.querySelector('[class*="question-title"], [class*="field-label"], [class*="form-label"], [class*="application-label"], legend, label');
  const marked = lab => {
    if (!lab) return false;
    if (/(^|[\s_-])required([\s_-]|$)/i.test(lab.className || '')) return true;
    if (STAR.test(txt(lab))) return true;
    if (lab.querySelector('[class*="required"], abbr[title*="required" i], [aria-label*="required" i]')) return true;
    try { if ((getComputedStyle(lab, '::after').content || '').includes('*')) return true; } catch (e) {}
    return false;
  };
  const entryRequired = el => {
    const box = entryOf(el); if (!box) return false;
    if (box.getAttribute('aria-required') === 'true') return true;
    return marked(titleOf(box));
  };
  // The question a control answers. A control's own label first, unless it
  // is a placeholder. For an option in a group, or a control whose label is
  // not wired up, the group's legend, its title element, or the first text
  // block inside it; failing all of that, the closest preceding text block.
  // Custom ATS widgets (Ashby, Greenhouse's react components) put the question
  // in a div, not a label.
  const questionFor = el => {
    const starOnly = s => /^[*✱:\s]+$/.test(s);
    if (!isOption(el)) { const own = ownLabel(el); if (own && !placeholderish(own) && !starOnly(own)) return own; }
    const grp = el.closest('fieldset, [role=group], [role=radiogroup], [data-field-path], [class*="field-entry"], [class*="application-question"]');
    if (grp) {
      const lg = grp.querySelector('legend'); if (lg && txt(lg)) return txt(lg).slice(0, MAXQ);
      if (grp.getAttribute('aria-label')) return grp.getAttribute('aria-label').trim();
      const lb = grp.getAttribute('aria-labelledby'); if (lb) { const t = byIds(lb); if (t) return t; }
      const optionTexts = new Set([...grp.querySelectorAll('input')].map(i => ownLabel(i)));
      const title = grp.querySelector('[class*="question-title"], [class*="field-label"], [class*="form-label"], [class*="application-label"]');
      if (title && txt(title) && !optionTexts.has(txt(title))) return txt(title).slice(0, MAXQ);
      for (const c of grp.querySelectorAll('label, legend, h1, h2, h3, h4, h5, h6, p, span, div')) {
        const t = txt(c);
        if (!t || t.length > MAXQ || c.querySelector('input, select, textarea') || optionTexts.has(t) || starOnly(t)) continue;
        return t;
      }
    }
    // The nearest preceding text block, climbing the ancestors. Nine levels:
    // Greenhouse's embedded react-select buries its input six wrappers below
    // the <label> that names it. A lone required star is not a name.
    const walkUp = (start, levels) => {
      let node = start;
      for (let depth = 0; depth < levels && node; depth++) {
        let sib = node.previousElementSibling;
        while (sib) {
          const t = txt(sib);
          if (t && t.length <= MAXQ && !sib.querySelector('input, select, textarea') && !placeholderish(t) && !/^[*✱:\s]+$/.test(t)) return t;
          sib = sib.previousElementSibling;
        }
        node = node.parentElement;
      }
      return '';
    };
    const walked = walkUp(el, 9);
    if (walked) return walked;
    // Inside a web component the question sits outside the shadow root
    // (SmartRecruiters' <spl-input> under its screening form): climb out
    // through each host and look around it the same way.
    // Twelve steps: SmartRecruiters nests a picker's input nine elements and
    // three shadow roots below the <spl-autocomplete> that carries its question.
    let host = el.getRootNode() && el.getRootNode().host;
    for (let depth = 0; depth < 12 && host; depth++) {
      const own = txt(host).replace(/\s*[*✱]\s*$/, '');
      if (own && own.length <= MAXQ && !placeholderish(own) && !/^[*✱:\s]+$/.test(own)) return own + (STAR.test(txt(host)) ? '*' : '');
      const around = walkUp(host, 6);
      if (around) return around + (STAR.test(txt(host)) ? '*' : '');
      const rn = host.getRootNode();
      host = host.parentElement || (rn && rn.host) || null;
    }
    // Last resort, the autofill tools' rule: the nearest <label> that comes
    // before the control in document order and names no control of its own
    // (Greenhouse's embed writes <label for="Country"> beside a react-select
    // whose input id is react-select-7-input).
    let best = null;
    for (const l of allLabels) {
      if (l.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) best = l; else break;
    }
    if (best && txt(best) && txt(best).length <= MAXQ && !placeholderish(txt(best))) return txt(best);
    return ownLabel(el) || el.placeholder || el.name || '';
  };
  const allLabels = document.querySelectorAll('label').filter(l => vis(l) && txt(l) && !l.querySelector('input:not([type=hidden]), select, textarea'));
  const tagOf = el => {
    if (!el.dataset.rtId) el.dataset.rtId = 'rt-' + (window.__rtSeq = (window.__rtSeq || 0) + 1);
    return el.dataset.rtId;
  };
  // The heading a control sits under — "Education", "Work History", "Voluntary
  // Self-Identification" — which is what tells an "End date" apart from another.
  const heads = [...document.querySelectorAll('h1, h2, h3, h4, h5, h6, legend, [class*="section-title"], [class*="sectionTitle"], [class*="section-heading"]')]
    .filter(h => vis(h) && txt(h) && txt(h).length <= 80);
  const sectionOf = el => {
    let last = '';
    for (const h of heads) {
      if (h.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) last = txt(h); else break;
    }
    return last;
  };
  // The format hint or description that goes with a control ("MM/DD/YYYY",
  // "Please enter your legal name").
  const hintOf = el => {
    const d = el.getAttribute('aria-describedby'); if (d) { const t = byIds(d); if (t) return t.slice(0, 160); }
    const box = entryOf(el);
    if (box) {
      const h = box.querySelector('[class*="hint"], [class*="description"], [class*="help"], [class*="subtitle"], small');
      if (h && txt(h) && !h.querySelector('input, select, textarea')) return txt(h).slice(0, 160);
    }
    return '';
  };
  // A picker's chosen entry. react-select clears its input once something is
  // picked and shows the choice in a sibling element, so the input's own
  // value says nothing; Ashby keeps the choice in the input.
  // A plain text input whose wrapper holds chosen chips beside it (Workday's
  // Country Phone Code keeps "United States of America (+1)" as a chip and
  // the input itself empty).
  const chipValue = el => {
    let n = el;
    for (let i = 0; i < 5 && n; i++) {
      n = n.parentElement;
      if (!n || n.tagName === 'FORM' || n.tagName === 'BODY') break;
      if (n.querySelectorAll('input, [role=combobox]').length > 2) break;
      const chips = [...n.querySelectorAll('[data-automation-id="selectedItem"], [data-automation-id*="selectedItem"]')].map(txt).filter(Boolean);
      if (chips.length) return chips.join(' | ');
    }
    return '';
  };
  const comboValue = el => {
    if (el.value && el.getAttribute('data-uxi-widget-type') !== 'selectinput' && !el.getAttribute('data-uxi-multiselect-id')) return el.value;
    let n = el;
    for (let i = 0; i < 5 && n; i++) {
      n = n.parentElement;
      if (!n || n.tagName === 'FORM' || n.tagName === 'BODY') break;
      if (n.querySelectorAll('input, [role=combobox]').length > 2) break;
      const v = n.querySelector('[class*="single-value"], [class*="selected-value"], [class*="selectedValue"]');
      if (v && txt(v)) return txt(v);
      // Workday's multi-select keeps its choices as chips beside the input.
      const chips = [...n.querySelectorAll('[data-automation-id="selectedItem"], [data-automation-id*="selectedItem"]')].map(txt).filter(Boolean);
      if (chips.length) return chips.join(' | ');
      if (/has-value/.test(n.className || '')) return '(selected)';
    }
    return '';
  };
  const sel = 'input:not([type=hidden]), select, textarea';
  // A styled radio or checkbox may be hidden outright behind its label; the
  // label being visible is what makes it a real choice on the page.
  const labelVisible = el => isOption(el) && el.labels && el.labels.length && vis(el.labels[0]);
  const out = [...document.querySelectorAll(sel)].filter(el => (vis(el) || labelVisible(el)) && !dummy(el)).map(el => {
    const id = tagOf(el);
    const type = (el.type || '').toLowerCase();
    const q = questionFor(el);
    const combo = (el.getAttribute('role') === 'combobox' || !!el.getAttribute('aria-autocomplete'))
      // Workday's searchable pickers are plain inputs beside a chip list (selectedItemList): a picker all the same.
      || !!(el.closest('[data-automation-id^="formField-"]') && el.closest('[data-automation-id^="formField-"]').querySelector('[data-automation-id="selectedItemList"], [data-automation-id="selectedItem"]'))
      // Workday's search-as-you-type selects carry their own widget markers.
      || el.getAttribute('data-uxi-widget-type') === 'selectinput' || !!el.getAttribute('data-uxi-multiselect-id')
      || (el.getAttribute('enterkeyhint') === 'search' && !!el.closest('[data-automation-id^="formField-"]'));
    const o = {
      id,
      selector: `[data-rt-id="${id}"]`,
      tag: el.tagName.toLowerCase(),
      type,
      label: q,
      name: el.name || '',
      dom_id: el.id || '',
      required: el.required || el.getAttribute('aria-required') === 'true' || STAR.test(q) || entryRequired(el),
      value: (type === 'file' || isOption(el)) ? ''
           : el.tagName === 'SELECT' ? (el.value && el.selectedOptions[0] ? txt(el.selectedOptions[0]) : '')
           : (combo ? comboValue(el) : (el.value || chipValue(el))),
      files: type === 'file' ? [...(el.files || [])].map(f => f.name) : undefined,
      combobox: combo,
      section: sectionOf(el),
      placeholder: el.placeholder || '',
      hint: hintOf(el),
      maxlength: el.maxLength > 0 ? el.maxLength : undefined,
    };
    if (o.tag === 'select') o.options = [...el.options].map(op => op.label || op.text).filter(Boolean);
    if (isOption(el)) { o.checked = el.checked; o.option_label = ownLabel(el) || el.value; o.group = el.name || ''; }
    // "Autofill my application" / "Autofill from resume" uploaders (Greenhouse,
    // Ashby) parse a résumé into the other fields; they are a convenience, never
    // a required field, whatever asterisk the page header lends them.
    if (type === 'file' && /autofill/i.test(q + ' ' + (o.hint || ''))) o.required = false;
    return o;
  });
  // Rich-text editors: a contenteditable box standing in for a textarea (some
  // cover-letter and "additional information" fields). Typed into, not filled.
  // Controls already reported by this scan (a tagged id from an earlier scan is not a reason to skip).
  const emitted = new Set(out.map(o => o.id));
  for (const ed of document.querySelectorAll('[contenteditable="true"], [contenteditable=""], [role=textbox]:not(input):not(textarea)')) {
    if (!vis(ed) || ed.closest('[contenteditable="true"]') !== ed && ed.getAttribute('contenteditable') !== 'true' && ed.getAttribute('role') !== 'textbox') continue;
    if (ed.matches('input, textarea')) continue;
    const id = tagOf(ed);
    const q = questionFor(ed);
    out.push({
      id, selector: `[data-rt-id="${id}"]`, tag: 'editor', type: 'editor', label: q, name: '', dom_id: ed.id || '',
      required: STAR.test(q) || entryRequired(ed) || ed.getAttribute('aria-required') === 'true',
      value: txt(ed), combobox: false, section: sectionOf(ed), placeholder: ed.getAttribute('data-placeholder') || '',
      hint: hintOf(ed), maxlength: undefined,
    });
  }
  // Custom dropdowns: a button that opens a listbox. What it offers is read
  // when it is opened; what it shows now is its value, unless that is a
  // placeholder or the question itself.
  for (const b of document.querySelectorAll('button[aria-haspopup="listbox"], [aria-haspopup="listbox"]:not(input), [role=combobox]:not(input):not(select)')) {
    if (!vis(b) || (b.dataset.rtId && emitted.has(b.dataset.rtId)) || b.closest('[role=listbox]')) continue;
    const id = tagOf(b);
    const q = questionFor(b);
    const shown = txt(b);
    out.push({
      id, selector: `[data-rt-id="${id}"]`, tag: 'listbox', type: 'listbox', label: q, name: '', dom_id: b.id || '',
      required: STAR.test(q) || entryRequired(b) || b.getAttribute('aria-required') === 'true',
      value: (placeholderish(shown) || shown === q || !shown || /^(select one|choose one|select|choose|--)$/i.test(shown)) ? '' : shown, combobox: true,
      section: sectionOf(b), placeholder: '', hint: hintOf(b), maxlength: undefined,
    });
  }
  // Yes/No toggles: two pressable buttons side by side, reported as one field
  // whose value is whichever button is pressed.
  const seen = new Set();
  for (const b of document.querySelectorAll('button[aria-pressed], [role=button][aria-pressed], [role=radio][aria-checked]')) {
    const t = txt(b).toLowerCase(); if (t !== 'yes' && t !== 'no') continue;
    const box = b.parentElement; if (!box || seen.has(box) || !vis(box)) continue;
    const opts = [...box.children].filter(x => ['yes', 'no'].includes(txt(x).toLowerCase()));
    if (opts.length !== 2) continue;
    seen.add(box);
    const id = tagOf(box);
    const on = opts.find(x => x.getAttribute('aria-pressed') === 'true' || x.getAttribute('aria-checked') === 'true');
    const q = questionFor(box);
    out.push({
      id, selector: `[data-rt-id="${id}"]`, tag: 'yesno', type: 'yesno', label: q, name: '', dom_id: '',
      required: STAR.test(q) || entryRequired(box), value: on ? txt(on) : '', options: opts.map(txt), combobox: false,
    });
  }
  return out;
}
"""
_FIELD_JS = _deep(_FIELD_JS)

# What a dropdown shows once something is picked: the input's own value
# (Ashby), or react-select's single-value element beside a cleared input.
# Climbs from the input only as far as its own widget — an ancestor holding
# other pickers would report their values.
_COMBO_VALUE_JS = r"""
e => {
  // A Workday search-select keeps the typed text in the input whether or
  // not anything was chosen; only its chips count.
  if (e.value && e.getAttribute('data-uxi-widget-type') !== 'selectinput' && !e.getAttribute('data-uxi-multiselect-id')) return e.value;
  let n = e;
  for (let i = 0; i < 6 && n; i++) {
    n = n.parentElement;
    if (!n || n.tagName === 'FORM' || n.tagName === 'BODY') break;
    if (n.querySelectorAll('input, [role=combobox]').length > 2) break;
    const v = n.querySelector('[class*="single-value"], [class*="selected-value"], [class*="selectedValue"]');
    if (v && (v.innerText || '').trim()) return v.innerText.trim();
    // A multi-select shows each chosen entry as a chip ("Active Security
    // Clearance(s)") rather than one single-value node.
    const chips = [...n.querySelectorAll('[class*="multi-value__label"], [class*="multi-value"] [class*="label"], [class*="multiValue"], spl-chip, [class*="-chip"], [data-automation-id="selectedItem"]')]
      .map(c => (c.innerText || '').trim()).filter(Boolean);
    if (chips.length) return [...new Set(chips)].join(' | ');
  }
  return '';
}
"""

# The picker's list as it stands: every option node's index (in document
# order, matching the same selector as a Playwright locator), its text, and
# whether it is the highlighted one. Hidden nodes — a phone widget's country
# list keeps 245 of them in the page — are left out.
# What a picker's list is made of: ARIA options, plain list items, and the
# web-component entries SmartRecruiters draws (spl-select-option inside a
# shadow root, rendered as a div.c-spl-autocomplete-…-option). Each entry is
# tagged so the click lands on the same node the scan saw, whatever order
# a shadow-piercing locator would put them in.
_OPTION_SEL = ('[role=option], [role=listbox] li, [id*="-option-"], [class*="__option"]:not([class*="__options"]), '
               '[class*="autocomplete-option"], [class*="autocomplete-default-option"], '
               'spl-select-option, spl-dropdown-item, [class*="dropdown-item"]')
_OPTIONS_JS = r"""
() => [...document.querySelectorAll('__OPTION_SEL__')].map((e, i) => {
  const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
  if (!e.dataset.rtOpt) e.dataset.rtOpt = 'opt-' + (window.__rtOptSeq = (window.__rtOptSeq || 0) + 1);
  return { i, id: e.dataset.rtOpt, t: (e.innerText || '').replace(/\s+/g, ' ').trim(),
           chip: !!(e.closest('[data-automation-id="selectedItem"], [data-automation-id*="selectedItem"]')),
           vis: r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none',
           focused: e.getAttribute('aria-selected') === 'true' || /focused|highlighted|active/i.test(String(e.className || '')) };
}).filter(o => o.vis && o.t && !o.chip).filter((o, k, all) => !all.some((p, j) => j < k && p.t === o.t))
""".replace("__OPTION_SEL__", _OPTION_SEL)
_OPTIONS_JS = _deep(_OPTIONS_JS)

# The page after a submit click: is the form still there, and what is it saying?
_AFTER_SUBMIT_JS = r"""
() => {
  const vis = el => {
    const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 0 && r.height > 0;
  };
  const fields = [...document.querySelectorAll('input:not([type=hidden]), select, textarea')].filter(vis);
  const submit = [...document.querySelectorAll('button, input[type=submit]')]
    .filter(b => vis(b) && /\b(submit|apply|send)\b/i.test(b.innerText || b.value || ''));
  const errors = [...document.querySelectorAll('[role=alert], [aria-live=assertive], [aria-live=polite], [class*="error" i], [class*="invalid" i], [aria-invalid=true]')]
    .filter(vis).map(e => (e.innerText || e.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim())
    .filter(t => t && t.length < 200);
  // A submit control that is disabled, marked busy, or showing a spinner is
  // still sending — nothing has been decided yet.
  const busy = submit.some(b => b.disabled || b.getAttribute('aria-busy') === 'true' || b.getAttribute('aria-disabled') === 'true'
    || /loading|busy|submitting|spinner|progress/i.test(b.className || '')
    || !!b.querySelector('[class*="spinner" i], [class*="loading" i], [class*="loader" i], [role=progressbar]'));
  return {
    form_present: fields.length >= 3 && submit.length > 0,
    fields: fields.length, submit: submit.length, busy,
    text: (document.body.innerText || '').slice(0, 20000),
    errors: [...new Set(errors)].slice(0, 12),
  };
}
"""
_AFTER_SUBMIT_JS = _deep(_AFTER_SUBMIT_JS)
# How long a submission may stay in flight before the tool stops waiting.
# A Greenhouse submit fetches a reCAPTCHA token and then posts; Ashby's
# spinner can run a good while. Two seconds — what the check used to allow —
# called both "not submitted" while they were still sending.
SUBMIT_WAIT_S = 75


def _submit_verdict(state: dict, url_changed: bool) -> tuple[bool, str] | None:
    """What the page after a submit click means: (submitted, why), or None
    while the submission is still in flight. A refusal is read before any
    sign of success, because Ashby replaces the whole form with its refusal
    banner and "the form is gone" alone proves nothing."""
    text = state.get("text") or ""
    refused = _FAILURE_TEXT.search(text)
    if refused:
        snippet = " ".join(text[max(0, refused.start() - 60): refused.end() + 140].split())
        return False, f"the site refused the submission: …{snippet}…"
    if _SUCCESS_TEXT.search(text):
        return True, "confirmation shown"
    if not state.get("form_present"):
        return True, "form gone after submit" + (", new page" if url_changed else "")
    if state.get("busy"):
        return None
    errors = [e for e in dict.fromkeys(state.get("errors") or []) if e]
    why = "submit was clicked but the form is still on the page"
    if errors:
        why += "; it says: " + " | ".join(errors)[:400]
    return False, why
_SUCCESS_TEXT = re.compile(
    r"thank you for (applying|your application|submitting)|application (has been |was )?(submitted|received|sent)"
    r"|successfully submitted|we('ve| have) received your application|your application (is|has been) (in|received|submitted)",
    re.I,
)
# What a site says when it turned the submission down. Checked before any
# sign of success: Ashby replaces the whole form with its refusal banner, so
# "the form is gone" alone is not proof of anything.
_FAILURE_TEXT = re.compile(
    r"couldn'?t submit|could not submit|unable to submit|was not submitted|flagged as (possible )?spam"
    r"|something went wrong|error (occurred|submitting)|submit(ting)? your application again|submission (failed|was rejected)",
    re.I,
)
_YES_WORDS = ("1", "true", "yes", "on", "checked")
# Inputs whose value is set as a whole, not typed key by key.
_FILL_ONLY_TYPES = {"date", "month", "week", "time", "datetime-local", "color", "range"}

# Validation messages the page is showing: what it wants changed before it
# will take the form.
_ERRORS_JS = r"""
() => {
  const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 0 && r.height > 0; };
  const txt = el => (el.innerText || el.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
  const out = [];
  for (const e of document.querySelectorAll('[role=alert], [aria-live=assertive], [class*="error" i]:not(input):not(select):not(textarea), [class*="invalid" i]:not(input):not(select):not(textarea)')) {
    if (!vis(e)) continue;
    const t = txt(e); if (t && t.length < 200) out.push(t);
  }
  for (const c of document.querySelectorAll('[aria-invalid=true]')) {
    const id = c.dataset && c.dataset.rtId; const msg = c.getAttribute('aria-errormessage');
    const m = msg && document.getElementById(msg);
    out.push((id ? id + ': ' : '') + ((m && txt(m)) || 'invalid'));
  }
  return [...new Set(out)].slice(0, 20);
}
"""
_ERRORS_JS = _deep(_ERRORS_JS)

_US_STATES = {
    "al": "alabama", "ak": "alaska", "az": "arizona", "ar": "arkansas", "ca": "california", "co": "colorado",
    "ct": "connecticut", "de": "delaware", "fl": "florida", "ga": "georgia", "hi": "hawaii", "id": "idaho",
    "il": "illinois", "in": "indiana", "ia": "iowa", "ks": "kansas", "ky": "kentucky", "la": "louisiana",
    "me": "maine", "md": "maryland", "ma": "massachusetts", "mi": "michigan", "mn": "minnesota",
    "ms": "mississippi", "mo": "missouri", "mt": "montana", "ne": "nebraska", "nv": "nevada",
    "nh": "new hampshire", "nj": "new jersey", "nm": "new mexico", "ny": "new york", "nc": "north carolina",
    "nd": "north dakota", "oh": "ohio", "ok": "oklahoma", "or": "oregon", "pa": "pennsylvania",
    "ri": "rhode island", "sc": "south carolina", "sd": "south dakota", "tn": "tennessee", "tx": "texas",
    "ut": "utah", "vt": "vermont", "va": "virginia", "wa": "washington", "wv": "west virginia",
    "wi": "wisconsin", "wy": "wyoming", "dc": "district of columbia",
}
_PLACE_NOISE = {"united", "states", "usa", "us", "america", "of", "the"}


def _place_tokens(text: str) -> set[str]:
    """A place name as a set of words, state codes written out and the
    country dropped, so "Durham, NC" and "Durham, North Carolina, United
    States" come out the same."""
    words = []
    for w in re.findall(r"[a-z]+", text.lower()):
        words.extend(_US_STATES.get(w, w).split())
    return {w for w in words if w not in _PLACE_NOISE}


_MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"])}
_MONTHS.update({m[:3]: i for m, i in list(_MONTHS.items())})
_MONTHS["sept"] = 9


def _parse_date(value: str) -> tuple[int, int | None, int | None] | None:
    """(year, month, day) from the ways a date is written in an answer bank or
    on a résumé: 'May 2028', 'May 1, 2028', '05/2028', '05/01/2028',
    '2028-05', '2028-05-01', '2028'. Month and day may be missing."""
    v = value.strip().lower().replace(",", " ")
    m = re.fullmatch(r"(\d{4})-(\d{1,2})(?:-(\d{1,2}))?", v)
    if m:
        return int(m[1]), int(m[2]), int(m[3]) if m[3] else None
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", v)
    if m:
        return int(m[3]), int(m[1]), int(m[2])
    m = re.fullmatch(r"(\d{1,2})/(\d{4})", v)
    if m:
        return int(m[2]), int(m[1]), None
    m = re.fullmatch(r"([a-z]+)\.?\s+(?:(\d{1,2})(?:st|nd|rd|th)?\s+)?(\d{4})", v)
    if m and m[1] in _MONTHS:
        return int(m[3]), _MONTHS[m[1]], int(m[2]) if m[2] else None
    m = re.fullmatch(r"(?:(\d{1,2})\s+)?([a-z]+)\.?\s+(\d{4})", v)
    if m and m[2] in _MONTHS:
        return int(m[3]), _MONTHS[m[2]], int(m[1]) if m[1] else None
    m = re.fullmatch(r"(\d{4})", v)
    if m:
        return int(m[1]), None, None
    return None


def _date_text(value: str, fmt_hint: str, typ: str) -> str:
    """`value` in the form a field wants: ISO for date/month inputs, else
    whatever pattern the field's hint spells out (MM/DD/YYYY, MM/YYYY...).
    A value that is not a date, or a field with no stated format, is left as is."""
    parsed = _parse_date(value)
    if not parsed:
        return value
    y, mo, d = parsed
    mo, d = mo or 1, d or 1
    if typ == "date":
        return f"{y:04d}-{mo:02d}-{d:02d}"
    if typ == "month":
        return f"{y:04d}-{mo:02d}"
    h = (fmt_hint or "").lower()
    if "mm/dd/yyyy" in h:
        return f"{mo:02d}/{d:02d}/{y}"
    if "dd/mm/yyyy" in h:
        return f"{d:02d}/{mo:02d}/{y}"
    if "mm/yyyy" in h:
        return f"{mo:02d}/{y}"
    if "yyyy-mm-dd" in h:
        return f"{y:04d}-{mo:02d}-{d:02d}"
    if "yyyy-mm" in h:
        return f"{y:04d}-{mo:02d}"
    return value


def _installed_channel() -> str | None:
    """The most convincing browser on this machine. Playwright's headless
    shell announces itself as HeadlessChrome, and Ashby's spam filter turned
    down a form submitted from it. The user's own Google Chrome, in Chrome's
    newer headless mode, looks like Chrome; Playwright's full Chromium build
    is the next best."""
    if sys.platform == "darwin" and Path("/Applications/Google Chrome.app").exists():
        return "chrome"
    if shutil.which("google-chrome") or shutil.which("google-chrome-stable"):
        return "chrome"
    return "chromium"

_SUBMIT_JS = """
() => {
  const vis = b => { const r = b.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const s = v => (typeof v === 'string' ? v : '');
  // Native buttons, ARIA buttons, and web-component buttons (SmartRecruiters'
  // <spl-button>, whose click target is a span — there is no <button> at all).
  const btnLike = el => el.matches('button, input[type=submit], [role=button]') || /-button$/i.test(el.tagName);
  const raw = document.querySelectorAll('*').filter(el => btnLike(el) && vis(el));
  // A component wrapping another button-like component reports once, as the
  // innermost — the one that takes the click.
  const within = (outer, inner) => { let n = inner.parentNode || inner.host; while (n) { if (n === outer) return true; n = n.parentNode || n.host; } return false; };
  const list = raw.filter(b => !raw.some(c => c !== b && within(b, c)));
  const textOf = b => (s(b.innerText) || s(b.value) || b.getAttribute('aria-label') || '').trim();
  // The native button inside a component gets its label by slotting, so its
  // own innerText is empty; the wrapping component carries the words.
  const outerText = b => { let n = b.parentNode || b.host; while (n) { if (n.nodeType === 1 && btnLike(n) && textOf(n)) return textOf(n); n = n.parentNode || n.host; } return ''; };
  return list.map((b, i) => {
    if (!b.dataset.rtBtn) b.dataset.rtBtn = 'btn-' + (window.__rtBtnSeq = (window.__rtBtnSeq || 0) + 1);
    return {
      id: b.dataset.rtBtn,
      selector: `[data-rt-btn="${b.dataset.rtBtn}"]`,
      text: (textOf(b) || outerText(b)).slice(0, 80),
      disabled: !!b.disabled || b.getAttribute('aria-disabled') === 'true',
      type: (b.getAttribute('type') || '').toLowerCase(),
      in_form: !!b.closest('form'),
    };
  }).filter(b => b.text);
}
"""
_SUBMIT_JS = _deep(_SUBMIT_JS)


_DECLINE_WORDS = ("decline", "prefer not", "do not wish", "don't wish", "not to answer",
                  "rather not", "choose not", "not to say", "not to disclose", "do not want",
                  "don't want", "not want to")


def _pick_option(value: str, options: list[dict]) -> int | None:
    """Index of the dropdown option that means `value`.

    Exact label or value first. Then a whole-word match, so "Yes" finds
    "Yes, I am authorized" but "No" does not find "Not applicable". Then the
    case that makes EEO fields answerable at all: every form words its decline
    option differently — "Decline to self-identify", "I don't wish to answer",
    "Prefer not to say" — and a decline in the answer bank should land on
    whichever of those the form offers.
    """
    v = value.strip().lower()
    labels = [(o.get("label") or "").strip().lower() for o in options]
    for i, (o, label) in enumerate(zip(options, labels)):
        if label == v or str(o.get("value", "")).strip().lower() == v:
            return i
    if v:
        # A whole-word match settles it only when one option has the word:
        # "Yes" against "Yes, no restrictions" and "Yes, with time
        # limitations" is a choice for whoever knows the answer, not for
        # list order.
        pat = re.compile(rf"\b{re.escape(v)}\b")
        hits = [i for i, label in enumerate(labels) if label and pat.search(label)]
        if len(hits) == 1:
            return hits[0]
    if any(w in v for w in _DECLINE_WORDS):
        for i, label in enumerate(labels):
            if any(w in label for w in _DECLINE_WORDS):
                return i
    return None


_APPLY_JS = """
() => [...document.querySelectorAll('*')]
  .filter(el => el.matches('a, button, [role=button], input[type=button], input[type=submit]') || /-button$/i.test(el.tagName))
  .filter(el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
  .map((el, i) => {
    if (!el.dataset.rtApply) el.dataset.rtApply = 'ap-' + (window.__rtApSeq = (window.__rtApSeq || 0) + 1);
    const s = v => (typeof v === 'string' ? v : '');
    return {
      selector: `[data-rt-apply="${el.dataset.rtApply}"]`,
      text: (s(el.innerText) || s(el.value) || el.getAttribute('aria-label') || '').trim().slice(0, 80),
      href: el.getAttribute('href') || '',
    };
  }).filter(x => x.text)
"""
_APPLY_JS = _deep(_APPLY_JS)
_POSTING_JS = """
() => {
  const sel = 'main, article, [role=main], #content, .content, #job, .job, .posting, .job-description,'
            + ' [class*="description"], [class*="posting"], [class*="job-content"], [class*="JobDescription"]';
  let best = document.body, bestLen = (document.body.innerText || '').length;
  for (const el of document.querySelectorAll(sel)) {
    const len = (el.innerText || '').length;
    if (len >= bestLen * 0.5 && len < bestLen) { best = el; bestLen = len; }
  }
  return (best.innerText || '').trim();
}
"""
_POSTING_JS = _deep(_POSTING_JS)
_APPLY_TEXT = re.compile(
    r"^\s*(apply( now| here| online| today)?( for this (job|position|role|internship|opening)( online)?"
    r"| to this (job|position|role)| for (the |this )?job( online)?)?|start( your| my| an)? application"
    r"|i['\u2019]m interested|continue to (the )?application|continue (your |my )?application|resume (your |my )?application)\s*[»›→>]*\s*$",
    re.I,
)
# Apply-like controls that lead away from the form: another site's autofill,
# job alerts, sharing.
_APPLY_EXCLUDE = re.compile(r"apply (with|using|via|through)|linkedin|indeed|seek|alert|filter|later|share|friend", re.I)
# The interstitial a portal puts between Apply and its form — Workday's
# "Apply Manually", a guest or e-mail route past account creation.
_APPLY_STEP2_TEXT = re.compile(
    r"^\s*(apply manually|apply (as|without)( an?)? (guest|account)|continue (as|without)( an?)? (guest|account)"
    r"|continue with email|apply with email|use (my )?email|i['\u2019]m new( here)?|new (user|candidate))\s*[»›→>]*\s*$",
    re.I,
)
# A cookie banner's accept button: one whose nearby text talks about cookies.
# A form's own consent controls sit in a container far larger than a banner.
_COOKIE_JS = """
() => {
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const ok = /^\\s*(accept( all)?( cookies)?|allow( all)?( cookies)?|i (accept|agree)|agree|got it|ok(ay)?|accept and (close|continue))\\s*$/i;
  for (const b of document.querySelectorAll('button, a, [role=button], input[type=button]')) {
    if (!vis(b)) continue;
    const t = ((typeof b.innerText === 'string' ? b.innerText : '') || (typeof b.value === 'string' ? b.value : '')).trim();
    if (!ok.test(t)) continue;
    let node = b.parentElement, depth = 0;
    while (node && depth < 8) {
      const txt = node.innerText || '';
      if (txt.length > 2500) break;
      if (/cookie|consent/i.test(txt)) { b.click(); return t; }
      node = node.parentElement; depth++;
    }
  }
  return '';
}
"""
_COOKIE_JS = _deep(_COOKIE_JS)


def _is_empty(field: dict) -> bool:
    """Whether a required field still needs an answer.

    A file input reports an empty `value` by design (browsers expose a fake
    path), so it is judged by its attached files instead.
    """
    if not field.get("required"):
        return False
    if field.get("type") == "file":
        return not field.get("files")
    if field.get("type") in ("checkbox", "radio"):
        return not field.get("checked")
    return not field.get("value")


@dataclass
class ApplySession:
    """One long-lived browser. Logins persist across runs via the profile dir."""

    profile_dir: Path = DEFAULT_PROFILE_DIR
    headless: bool = False
    channel: str | None = None  # "chrome" | "chromium" | "" for the headless shell; None picks
    fast: bool = False  # set values outright instead of typing them: for a window a person will submit
    _pw: Any = field(default=None, repr=False)
    _ctx: Any = field(default=None, repr=False)
    _page: Any = field(default=None, repr=False)
    _frame: Any = field(default=None, repr=False)  # the iframe holding the form, when there is one
    _declined: set = field(default_factory=set, repr=False)  # lone checkboxes answered No: unticked on purpose
    _gh_slugs: set = field(default_factory=set, repr=False)  # Greenhouse boards the pages loaded talked to
    _uploaded: set = field(default_factory=set, repr=False)  # file fields (selector or label) that took a file

    @property
    def _doc(self):
        """Where the form lives: the page, or the embedded frame that holds it
        (a Greenhouse board inside a company's own careers page)."""
        return self._frame or self._page

    async def _pick_frame(self) -> None:
        count = _COUNT_JS
        self._frame = None
        try:
            main_n = await self._page.evaluate(count)
        except Exception:
            main_n = 0
        best, best_n = None, main_n
        for fr in self._page.frames[1:]:
            try:
                n = await fr.evaluate(count)
            except Exception:
                continue
            if n > best_n:
                best, best_n = fr, n
        self._frame = best

    async def start(self) -> None:
        if self._ctx is not None:
            return
        from playwright.async_api import async_playwright

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()
        launch = dict(
            headless=self.headless,
            viewport={"width": 1400, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        channel = self.channel if self.channel is not None else _installed_channel()
        if channel:
            launch["channel"] = channel

        async def open_context(**extra):
            # Persistent context, not launch() — cookies and sessions are the
            # whole point. Log in by hand once and every later run is already
            # authenticated.
            try:
                return await self._pw.chromium.launch_persistent_context(str(self.profile_dir), **launch, **extra)
            except Exception:
                if not launch.get("channel"):
                    raise
                # That browser is not installed after all; the bundled one will do.
                launch.pop("channel")
                return await self._pw.chromium.launch_persistent_context(str(self.profile_dir), **launch, **extra)

        try:
            self._ctx = await open_context()
        except Exception as e:
            if "existing browser session" not in str(e) and "already in use" not in str(e):
                raise
            # Another window holds this profile — a review of one posting
            # beside a review of another. Take a sibling directory of our own.
            import os

            self.profile_dir = Path(f"{self.profile_dir}-{os.getpid()}")
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            self._ctx = await open_context()
        page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        ua = await page.evaluate("navigator.userAgent")
        if "HeadlessChrome" in ua:
            # Even Chrome's own headless mode signs its requests HeadlessChrome,
            # in the user agent and the client-hint brands, and a site's bot
            # score reads that first. The same browser, introduced as itself.
            await self._ctx.close()
            self._ctx = await open_context(user_agent=ua.replace("HeadlessChrome", "Chrome"))
            page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        self._page = page
        # An element that a re-render removed should fail in seconds, not 30.
        self._page.set_default_timeout(10000)
        await self._load_logins()
        # A company careers page that embeds a Greenhouse board fetches it by
        # board slug; noting those calls tells greenhouse_embed_url() the
        # slug when the page's markup never spells it out.
        try:
            self._ctx.on("request", self._note_request)
        except Exception:
            pass
        try:
            tells = await self._page.evaluate(
                "() => ({ua: navigator.userAgent, webdriver: navigator.webdriver, plugins: navigator.plugins.length,"
                " brands: (navigator.userAgentData && navigator.userAgentData.brands || []).map(b => b.brand).join('/'),"
                " chrome: !!window.chrome})")
            print(f"[browser] {launch.get('channel') or 'headless shell'}, {'headless' if self.headless else 'headed'}: {tells}",
                  file=sys.stderr, flush=True)
        except Exception:
            pass

    LOGINS_DIR = DEFAULT_PROFILE_DIR.parent / "logins"  # ~/.resume-tailor/logins/<host>.json

    async def save_logins(self) -> int:
        """Keep this browser's cookies — the sign-in the user just did — so
        every later browser, headless included, starts signed in there.
        One file per site host; returns how many cookies were saved."""
        state = await self._ctx.storage_state()
        cookies = [c for c in state.get("cookies", []) if c.get("domain")]
        self.LOGINS_DIR.mkdir(parents=True, exist_ok=True)
        by_host: dict[str, list] = {}
        for c in cookies:
            by_host.setdefault(c["domain"].lstrip("."), []).append(c)
        for host, cs in by_host.items():
            path = self.LOGINS_DIR / (re.sub(r"[^a-z0-9.-]", "_", host.lower()) + ".json")
            old = []
            if path.is_file():
                try:
                    old = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    old = []
            keep = {(c["name"], c.get("path", "/")): c for c in old}
            keep.update({(c["name"], c.get("path", "/")): c for c in cs})
            path.write_text(json.dumps(list(keep.values())), encoding="utf-8")
        return len(cookies)

    async def _load_logins(self) -> None:
        """Cookies saved by save_logins(), added to this context on start."""
        if not self.LOGINS_DIR.is_dir():
            return
        cookies: list[dict] = []
        for path in self.LOGINS_DIR.glob("*.json"):
            try:
                cookies.extend(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                continue
        fresh = [c for c in cookies if not c.get("expires") or c["expires"] < 0 or c["expires"] > time.time()]
        if fresh:
            try:
                await self._ctx.add_cookies(fresh)
            except Exception:
                pass

    async def reset(self) -> None:
        """Between postings: one tab, blank. An Apply that opened a new tab
        leaves the old one behind, and thirty postings later the browser is
        carrying a tab per posting and timing out on page loads."""
        if self._ctx is None:
            return
        pages = list(self._ctx.pages)
        keep = pages[0] if pages else await self._ctx.new_page()
        for p in pages[1:]:
            try:
                await p.close()
            except Exception:
                pass
        self._page = keep
        self._page.set_default_timeout(10000)
        self._frame = None
        self._declined.clear()
        self._uploaded.clear()
        try:
            await self._page.goto("about:blank", timeout=5000)
        except Exception:
            pass

    async def stop(self) -> None:
        if self._ctx:
            await self._ctx.close()
            self._ctx = None
        if self._pw:
            await self._pw.stop()
            self._pw = None

    async def goto(self, url: str) -> str:
        await self.start()
        # Thirty seconds for the navigation itself: SuccessFactors and Workday
        # answer slowly, and a timeout here costs the whole attempt.
        await self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
        # ATS pages render the posting and the form from JavaScript after load.
        # Wait for the network to go quiet, but not forever — analytics beacons
        # on some sites never stop.
        try:
            await self._page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        await self._page.wait_for_timeout(600)
        await self._pick_frame()
        await self._settle()
        return await self._page.title()

    async def read_text(self) -> str:
        await self.start()
        return await self._doc.evaluate("() => document.body.innerText")

    async def errors(self) -> list[str]:
        """Validation messages the form is showing right now."""
        await self.start()
        try:
            return await self._doc.evaluate(_ERRORS_JS)
        except Exception:
            return []

    async def read_posting_text(self, max_chars: int = 15000) -> str:
        """The posting itself, not the whole page.

        Picks the smallest container that still holds most of the page's text,
        which drops navigation, footers and "other openings" lists, then caps
        the length: a job description is a few thousand characters, and what
        follows is boilerplate that only costs tokens — and on a tight rate
        limit, tokens are the budget.
        """
        await self.start()
        text = await self._page.evaluate(_POSTING_JS)
        text = re.sub(r"\n{3,}", "\n\n", text or "")
        return text[:max_chars]

    async def describe_form(self) -> list[dict]:
        await self.start()
        fields = await self._doc.evaluate(_FIELD_JS)
        _relabel_workday(fields)
        return fields

    async def buttons(self) -> list[dict]:
        await self.start()
        return await self._doc.evaluate(_SUBMIT_JS)

    async def click_apply_control(self) -> bool:
        """Click the page's Apply control, then the interstitial behind it if
        the form has still not appeared (Workday's "Apply Manually", a guest
        route past account creation). Returns whether anything was clicked.

        Several distinct Apply controls usually mean a listing of many jobs
        rather than one posting, and nothing is clicked blind; but the same
        link repeated at the top and bottom of a posting, or a specific
        "Apply to this job" beside a generic "Apply", is one choice."""
        await self.start()
        try:
            seen = {c["selector"] for c in await self._doc.evaluate(_APPLY_JS)}
        except Exception:
            seen = set()
        baseline = await self._fields_present()
        first = await self._click_control(_APPLY_TEXT)
        if not first:
            return False
        if not await self._wait_for_fields(10, baseline):
            # A split button ("Apply now ▾") opens a menu whose own "Apply
            # now" item was not on the page before; a portal's interstitial
            # ("Apply Manually") is the other shape of the same delay. Only
            # a control that appeared after the click is worth a second one.
            if (await self._click_control(_APPLY_TEXT, exclude=seen)
                    or await self._click_control(_APPLY_STEP2_TEXT, exclude=seen)):
                await self._wait_for_fields(8, baseline)
        return True

    async def _click_control(self, pattern: re.Pattern, exclude: set[str] | None = None) -> str | None:
        """Click the one control whose text matches; returns its selector."""
        try:
            controls = await self._doc.evaluate(_APPLY_JS)
        except Exception:
            return None
        distinct: dict[str, dict] = {}
        for c in controls:
            # An icon's accessible name repeats the label ("Apply Apply").
            text = re.sub(r"\b(\w+)(\s+\1\b)+", r"\1", c.get("text") or "", flags=re.I)
            if not pattern.match(text) or _APPLY_EXCLUDE.search(text) or c["selector"] in (exclude or ()):
                continue
            key = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
            # The same wording as a link and as a button: keep the one with a target.
            if key not in distinct or (c.get("href") and not distinct[key].get("href")):
                distinct[key] = c
        cands = list(distinct.values())
        if not cands:
            return None
        chosen = cands[0]
        if len(cands) > 1:
            ranked = sorted(cands, key=lambda c: len(c["text"]), reverse=True)
            hrefs = {(c.get("href") or "").split("?")[0].split("#")[0] for c in cands if c.get("href")}
            if len(ranked[0]["text"]) >= len(ranked[1]["text"]) + 4:
                chosen = ranked[0]  # the more specific wording
            elif len(hrefs) == 1:
                chosen = next(c for c in cands if c.get("href"))  # one destination, several labels
            else:
                return None
        before = list(self._ctx.pages)
        try:
            await self._doc.locator(chosen["selector"]).first.click()
        except Exception:
            return None
        try:
            await self._page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        await self._page.wait_for_timeout(900)
        # Some career sites open the application in a new tab; the form is
        # there, not on the page that was clicked.
        opened = [p for p in self._ctx.pages if p not in before]
        if opened:
            self._page = opened[-1]
            self._page.set_default_timeout(10000)
            try:
                await self._page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            await self._page.wait_for_timeout(600)
            await self._dismiss_cookie_banner()
        # No banner dismissal on the same page: an "Accept all" click lands
        # outside the menu a split Apply button has just opened, and closes it.
        await self._pick_frame()
        return chosen["selector"]

    async def _close_menu(self, el) -> None:
        """A multi-select keeps its list open after a choice; the next click
        anywhere (Save and Continue) would only close it. Escape, then Tab
        away, and the choice stays."""
        try:
            await self._page.keyboard.press("Escape")
            await self._page.wait_for_timeout(200)
            still = await self._visible_options()
            if still:
                await el.press("Tab")
                await self._page.wait_for_timeout(200)
        except Exception:
            pass

    async def _fields_present(self) -> int:
        await self._pick_frame()
        try:
            return int(await self._doc.evaluate(
                _COUNT_JS))
        except Exception:
            return 0

    async def _wait_for_fields(self, seconds: float = 10.0, baseline: int | None = None) -> bool:
        """Poll for a form to render: a portal draws its application from
        JavaScript well after the page loads, and an Apply click may open a
        drawer or an embedded board that takes a few seconds to arrive.
        `baseline` is the field count before the click — a page whose only
        inputs are its job-alert box still has them afterwards, and that is
        not the form arriving."""
        deadline = time.monotonic() + seconds
        while True:
            n = await self._fields_present()
            if n and (baseline is None or n != baseline):
                return True
            if n and baseline is not None:
                try:
                    if _looks_like_application(await self.describe_form()):
                        return True
                except Exception:
                    pass
            if time.monotonic() > deadline:
                return False
            await self._page.wait_for_timeout(500)

    async def _settle(self) -> None:
        """After a navigation: clear a cookie banner that would sit over the
        page, and give a slow single-page app a few seconds to show either
        its form or its Apply control."""
        await self._dismiss_cookie_banner()
        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline:
            if await self._fields_present():
                break
            try:
                controls = await self._doc.evaluate(_APPLY_JS)
            except Exception:
                controls = []
            if any(_APPLY_TEXT.match(c.get("text") or "") for c in controls):
                break
            await self._page.wait_for_timeout(500)
        await self._pick_frame()

    async def _dismiss_cookie_banner(self) -> None:
        try:
            clicked = await self._page.evaluate(_COOKIE_JS)
        except Exception:
            return
        if clicked:
            await self._page.wait_for_timeout(400)

    async def page_html(self) -> str:
        await self.start()
        try:
            return await self._page.content()
        except Exception:
            return ""

    def _note_request(self, request) -> None:
        try:
            m = re.search(r"greenhouse\.io/(?:v1/boards/|embed/job_(?:board|app)[^\s]*?[?&]for=)([A-Za-z0-9_-]+)", request.url)
        except Exception:
            return
        if m:
            self._gh_slugs.add(m.group(1))

    async def greenhouse_embed_url(self, url: str) -> str | None:
        """A company page that embeds a Greenhouse board (…?gh_jid=123) names
        its board slug in the embed script it loads; the application itself
        lives at boards.greenhouse.io/embed/job_app?for=<slug>&token=<id>."""
        m = re.search(r"[?&]gh_jid=(\d+)", url)
        if not m:
            return None
        slug = re.search(r"greenhouse\.io/embed/job_(?:board|app)[^\"'\s]*?[?&]for=([A-Za-z0-9_-]+)", await self.page_html())
        name = slug.group(1) if slug else (next(iter(self._gh_slugs)) if len(self._gh_slugs) == 1 else None)
        if not name:
            # Greenhouse resolves the board from the token alone: this URL
            # redirects to …/embed/job_app?for=<slug>&token=<id>.
            return f"https://boards.greenhouse.io/embed/job_app?token={m.group(1)}"
        return f"https://boards.greenhouse.io/embed/job_app?for={name}&token={m.group(1)}"

    async def _locate(self, selector: str, field: dict | None = None):
        """The element the scanner tagged — or, when a React re-render has
        replaced it since, the same field found again by its DOM id, its name,
        or its question."""
        doc = self._doc
        loc = doc.locator(selector).first
        if await loc.count():
            return loc
        if field:
            dom_id = field.get("dom_id") or ""
            if dom_id:
                cand = doc.locator(f'[id="{dom_id}"]').first
                if await cand.count():
                    return cand
            for f in await self.describe_form():
                if f.get("type") != field.get("type") or f.get("option_label", "") != field.get("option_label", ""):
                    continue
                if (field.get("name") and f.get("name") == field["name"]) or f.get("label") == field.get("label"):
                    return doc.locator(f["selector"]).first
        raise LookupError(f"the field {field.get('label') if field else selector!r} is no longer on the page")

    async def fill(self, selector: str, value: str, field: dict | None = None) -> None:
        """Put `value` into one control. `field` is the scanner's record for it,
        used to find the control again if the page re-rendered it and to know
        what kind of widget it is."""
        await self.start()
        el = await self._locate(selector, field)
        tag = await el.evaluate("e => e.tagName.toLowerCase()")
        typ = (await el.evaluate("e => (e.type || '').toLowerCase()")) or ""
        if tag not in ("input", "select", "textarea"):
            if (field or {}).get("tag") == "listbox" or await el.evaluate(
                    "e => e.getAttribute('aria-haspopup') === 'listbox' || e.getAttribute('role') === 'combobox'"):
                await self._pick_listbox(el, value)
                return
            if (field or {}).get("tag") == "editor" or await el.evaluate("e => e.isContentEditable || e.getAttribute('role') === 'textbox'"):
                # A rich-text editor: select what is there, type over it.
                await el.scroll_into_view_if_needed()
                await el.click()
                await self._page.keyboard.press("Meta+A" if sys.platform == "darwin" else "Control+A")
                await self._page.keyboard.press("Backspace")
                await self._page.keyboard.type(value, delay=6)
                return
            await self._press_yes_no(el, value)
            return
        # Dates, in the form this field wants them.
        fmt_hint = " ".join(str((field or {}).get(k) or "") for k in ("placeholder", "hint", "label"))
        if typ in ("date", "month") or re.search(r"yyyy|mm/", fmt_hint, re.I):
            value = _date_text(value, fmt_hint, typ)
        # The scanner's verdict counts too: Workday's search-selects carry
        # none of the ARIA markers, only their own widget attributes.
        is_combobox = bool(field and field.get("combobox")) or await el.evaluate(
            "e => e.getAttribute('role') === 'combobox' || !!e.getAttribute('aria-autocomplete')"
            " || e.getAttribute('data-uxi-widget-type') === 'selectinput' || !!e.getAttribute('data-uxi-multiselect-id')"
        )
        if tag == "input" and is_combobox:
            await self._pick_combobox(el, value)
            return
        phone_label = bool(re.search(r"\bphone\b", (field or {}).get("label") or "", re.I)) and not re.search(
            r"extension|\bext\b|code|type|device", (field or {}).get("label") or "", re.I)
        if (typ == "tel" or phone_label) and re.match(r"^\s*\+?1\b", value) and await self._doc.evaluate(
                "() => !!document.querySelector('[data-automation-id=\"formField-countryPhoneCode\"], [data-automation-id*=\"countryPhoneCode\"]')"):
            # The country code has its own field here (Workday): the number
            # takes the national digits alone.
            digits = re.sub(r"\D", "", value)
            value = digits[1:] if len(digits) == 11 and digits.startswith("1") else digits
        if tag == "select":
            options = await el.evaluate(
                "e => [...e.options].map(o => ({label: (o.label || o.text || '').trim(), value: o.value}))"
            )
            i = _pick_option(value, options)
            if i is None:
                raise ValueError(
                    f"no option matches {value!r}; the dropdown offers "
                    f"{[o['label'] for o in options if o['label']][:10]}"
                )
            await el.select_option(index=i)
        elif typ in ("checkbox", "radio"):
            want = str(value).strip().lower() in _YES_WORDS
            if typ == "checkbox" and not want:
                # An opt-in toggle ("communicate by phone instead?") answered
                # No stays unticked — that is its answer, not an omission.
                self._declined.add(selector)
            try:
                await (el.check() if want else el.uncheck())
            except Exception:
                # A styled control (Ashby's zero-opacity radios) can refuse a
                # direct click; its label takes the click instead.
                await el.evaluate("e => { const l = e.labels && e.labels[0]; (l || e).click(); }")
                await self._page.wait_for_timeout(200)
                if (await el.is_checked()) != want:
                    raise
        elif typ == "number":
            # A numeric input refuses anything but digits — a phone number
            # typed as "+1 910-336-0632" is rejected outright. A range or a
            # sentence ("$30-40 per hour") is not a number at all, and is
            # refused rather than mangled into 3040.
            digits = re.sub(r"\D", "", value)
            numbers = re.findall(r"\d+(?:\.\d+)?", value)
            if len(digits) >= 10:
                if len(digits) == 11 and digits.startswith("1") and re.search(r"[+\-() ]", value):
                    digits = digits[1:]  # a phone with its +1 country code
            elif len(numbers) != 1:
                raise ValueError(f"{value!r} is not a single number")
            else:
                digits = numbers[0]
            if not digits:
                raise ValueError(f"{value!r} has no digits for a numeric field")
            await self._type(el, digits)
        elif typ in _FILL_ONLY_TYPES:
            await el.fill(value)
        else:
            await self._type(el, value)

    async def _type(self, el, value: str) -> None:
        """Enter text the way a person does — the pointer goes to the field,
        the keys go in one at a time — rather than setting the value in one
        stroke. The bot score behind a submit button is built from exactly
        these signals."""
        # The pointer's approach is a courtesy to the bot score, not a
        # requirement: a field a sticky header covers (Workday) still takes
        # a forced click and its text.
        try:
            await el.scroll_into_view_if_needed(timeout=3000)
            await el.hover(timeout=3000)
        except Exception:
            pass
        try:
            await el.click(timeout=4000)
        except Exception:
            await el.click(force=True, timeout=4000)
        await el.fill("")
        if not value:
            return
        if self.fast:
            # A review window: the person at the keyboard will press Submit,
            # so the bot score at submit time is theirs, not ours.
            await el.fill(value)
        else:
            # Short answers at a typing pace; an essay faster, or a form takes minutes.
            await el.press_sequentially(value, delay=random.randint(30, 55) if len(value) <= 120 else 6)
        # A plain text box with a suggestion list attached by script (Lever's
        # "Name of School") clears what was typed unless a suggestion is
        # chosen. If a short list appeared, choose the entry that means it.
        if len(value) <= 120:
            found = await self._visible_options(limit=30, wait_ms=600)
            if found and len(found) <= 30:
                i = self._match_option(value, found)
                if i is not None:
                    try:
                        await self._doc.locator(f'[data-rt-opt="{found[i]["id"]}"]').first.click()
                        await self._page.wait_for_timeout(250)
                    except Exception:
                        pass

    async def _pick_listbox(self, el, value: str) -> None:
        """A custom dropdown — a button that opens a list — chosen from by
        opening it and clicking the entry that means `value`."""
        page = self._page
        await el.scroll_into_view_if_needed()
        await el.click()
        found = await self._visible_options()
        i = self._match_option(value, found) if found else None
        if i is None:
            await page.keyboard.press("Escape")
            raise ValueError(f"no option matches {value!r}"
                             + (f"; it offered {[o['t'] for o in found[:8]]}" if found else "; the list showed nothing"))
        await self._doc.locator(f'[data-rt-opt="{found[i]["id"]}"]').first.click()
        await page.wait_for_timeout(300)

    async def _press_yes_no(self, box, value: str) -> None:
        """Answer a two-button Yes/No widget and confirm the button took."""
        want = "yes" if str(value).strip().lower() in _YES_WORDS else "no"
        btn = box.locator("button, [role=radio], [role=button]").filter(has_text=re.compile(rf"^\s*{want}\s*$", re.I)).first
        if not await btn.count():
            raise ValueError(f"the yes/no widget has no {want!r} button")
        await btn.click()
        await self._page.wait_for_timeout(250)
        state = await btn.evaluate("b => b.getAttribute('aria-pressed') || b.getAttribute('aria-checked') || ''")
        if state != "true":
            raise ValueError(f"the {want!r} button did not register")

    async def _visible_options(self, limit: int = 60, wait_ms: int = 3000) -> list[dict]:
        """The picker's visible list — polled, because react-select and Ashby
        fetch some lists as you type, and a list that has not arrived yet
        looks the same as a list with nothing in it."""
        import time

        deadline = time.monotonic() + wait_ms / 1000
        while True:
            found = await self._doc.evaluate(_OPTIONS_JS)
            if found or time.monotonic() > deadline:
                return found[: limit + 1]
            await self._page.wait_for_timeout(250)

    @staticmethod
    def _match_option(value: str, found: list[dict], loose_prefix: str | None = None) -> int | None:
        """Which listed entry means `value`: exact or whole-word first, then
        one containing the other ("Computer Science" for "Computer Science and
        Mathematics"), then the same place written out ("Durham, NC" for
        "Durham, North Carolina, United States"), then — when a single word
        was typed — the one entry starting with it, so "Bachelor" lands on
        "Bachelor's Degree" but "May" does not pick among many."""
        i = _pick_option(value, [{"label": o["t"], "value": o["t"]} for o in found])
        if i is not None:
            return i
        v = value.strip().lower()
        # One containing the other — but only when exactly one entry does:
        # "Durham" against two Durhams is a question, not a match.
        holds = [k for k, o in enumerate(found) if v and (v in o["t"].lower() or o["t"].lower() in v)]
        i = holds[0] if len(holds) == 1 else None
        if i is None:
            want = _place_tokens(value)
            if want:
                same = [k for k, o in enumerate(found) if want <= _place_tokens(o["t"])]
                i = same[0] if len(same) == 1 else None
        if i is None and loose_prefix:
            # The one entry starting with the typed word — and every other
            # significant word of the value must be in it too, so "Duke"
            # does not land on "Duke-NUS Medical School" for "Duke University".
            words = [w for w in re.findall(r"[A-Za-z0-9]+", value.lower()) if len(w) >= 3]
            starts = [k for k, o in enumerate(found) if o["t"].lower().startswith(loose_prefix.lower())
                      and all(w in o["t"].lower() for w in words)]
            i = starts[0] if len(starts) == 1 else None
        if i is None and len(holds) > 1 and re.fullmatch(r"\d+(\.\d+)?", v):
            # A bare score against "1560 out of 1600" and "1560 out of 2400":
            # the tightest scale that holds the score is the one it was
            # scored on.
            scales = []
            for k in holds:
                m = re.search(r"(?:out of|/)\s*(\d+(?:\.\d+)?)", found[k]["t"], re.I)
                if m and float(m.group(1)) >= float(v):
                    scales.append((float(m.group(1)), k))
            if len(scales) == len(holds):
                i = min(scales)[1]
        return i

    async def _pick_combobox(self, el, value: str) -> None:
        """Choose `value` in a search-as-you-type picker (Ashby's autocomplete,
        Greenhouse's react-select). Type it, read the list that appears, click
        the entry that means it; if the full text filters everything out, try
        the first word alone. Only an entry whose text was matched is ever
        clicked — a picker offering nothing that means the value is a miss,
        not a guess — and the widget is checked afterwards for a value."""
        page = self._page
        first_word = next((w for w in re.findall(r"[A-Za-z0-9']+", value) if len(w) >= 3), value[:8])
        offered: list[str] = []
        # The value as typed; its first word alone; and the untyped full list,
        # for an answer worded differently from every entry ("Decline to
        # self-identify" against "I don't wish to answer").
        for attempt, typed in enumerate((value[:80], first_word, "")):
            if attempt == 1 and typed.lower() == value.strip().lower():
                continue  # a one-word value has no shorter form to try
            await el.click()
            await el.fill("")
            if typed:
                await el.press_sequentially(typed, delay=30)
            found = await self._visible_options()
            if not found or (attempt == 2 and len(found) > 60):
                continue
            offered = offered or [o["t"] for o in found[:8]]
            i = self._match_option(value, found, loose_prefix=first_word if attempt == 1 else None)
            if i is None:
                continue
            # A virtualized list (Workday) recycles its rows as the filter
            # settles, so the row read a moment ago may show another entry
            # now: click only a row whose text still reads as matched.
            wanted = found[i]["t"]
            opt = self._doc.locator(f'[data-rt-opt="{found[i]["id"]}"]').first
            try:
                live = " ".join((await opt.inner_text()).split())
            except Exception:
                live = ""
            if live != wanted:
                current = await self._visible_options()
                j = next((k for k, o in enumerate(current) if o["t"] == wanted), None)
                if j is None:
                    continue
                opt = self._doc.locator(f'[data-rt-opt="{current[j]["id"]}"]').first
            before_texts = [o["t"] for o in found]
            await opt.click()
            await page.wait_for_timeout(500)
            if await el.evaluate(_COMBO_VALUE_JS):
                await self._close_menu(el)
                return
            # A hierarchical prompt (Workday's "How did you hear": Job Boards →
            # Indeed, Handshake, Other…): the click opened a branch, and a
            # leaf must be chosen — the one that means the value, else Other,
            # else the first.
            leaves = await self._visible_options()
            leaf_texts = [o["t"] for o in leaves]
            if leaves and leaf_texts != before_texts and not any(l == wanted for l in leaf_texts):
                j = self._match_option(value, leaves)
                if j is None:
                    j = next((k for k, o in enumerate(leaves) if re.fullmatch(r"other|others|not listed", o["t"], re.I)), 0)
                leaf = self._doc.locator(f'[data-rt-opt="{leaves[j]["id"]}"]').first
                try:
                    live = " ".join((await leaf.inner_text()).split())
                except Exception:
                    live = ""
                if live == leaves[j]["t"]:
                    await leaf.click()
                    await page.wait_for_timeout(500)
                    if await el.evaluate(_COMBO_VALUE_JS):
                        await self._close_menu(el)
                        return
            # A multi-select keeps its menu open; the chosen chip shows once it closes.
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(300)
            if await el.evaluate(_COMBO_VALUE_JS):
                return
            # The click landed but the widget did not take it (some
            # react-select builds commit only through the keyboard): type the
            # matched entry's own text and choose it with the arrow keys.
            await el.click()
            await el.fill("")
            await el.fill(found[i]["t"][:80])
            await page.wait_for_timeout(500)
            await page.keyboard.press("ArrowDown")
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(350)
            if await el.evaluate(_COMBO_VALUE_JS):
                return
        # Last try: type it and press Enter — some pickers commit the typed
        # entry — and keep it only if the widget then shows that entry.
        try:
            await el.click()
            await el.fill("")
            await el.press_sequentially(value[:40], delay=25)
            await page.wait_for_timeout(600)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(500)
            shown = await el.evaluate(_COMBO_VALUE_JS)
            if shown and _pick_option(value, [{"label": shown, "value": shown}]) is not None:
                return
            await el.fill("")
        except Exception:
            pass
        await page.keyboard.press("Escape")
        raise ValueError(f"no option matches {value!r} in this picker"
                         + (f"; it offered {offered}" if offered else "; it showed no list"))

    async def combobox_options(self, selector: str, field: dict | None = None, limit: int = 60) -> list[str]:
        """What a picker offers when opened with nothing typed — the short,
        fixed lists ("Where did you hear about us?", graduation month). A
        search-backed picker (School, with thousands of entries) shows nothing
        or too much, and is reported as having no fixed options."""
        await self.start()
        page = self._page
        el = await self._locate(selector, field)
        await el.click()
        found = await self._visible_options(limit, wait_ms=800)
        if not found:
            # Ashby and Workday open the list on input rather than on focus:
            # type a character, read the list while it shows, take it back.
            await el.fill("a")
            found = await self._visible_options(limit, wait_ms=1500)
            await el.fill("")
            if not found:
                found = await self._visible_options(limit, wait_ms=800)
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(150)
        labels = [o["t"] for o in found]
        return labels if 0 < len(labels) <= limit else []

    async def submit(self, selector: str) -> tuple[bool, str]:
        """Click the submit control, then check that the form actually went:
        a confirmation message, or the form gone from the page — rather than
        the form still standing with validation errors under it. Returns
        (submitted, why)."""
        await self.start()
        page = self._page
        before = page.url
        button = self._doc.locator(selector).first
        await button.scroll_into_view_if_needed()
        box = await button.bounding_box()
        if box:
            # The pointer travels to the button, as a hand would move it.
            await page.mouse.move(box["x"] + box["width"] * random.uniform(0.3, 0.7),
                                  box["y"] + box["height"] * random.uniform(0.3, 0.7),
                                  steps=random.randint(12, 25))
            await page.wait_for_timeout(random.randint(200, 500))
        await button.click()
        import time

        deadline = time.monotonic() + SUBMIT_WAIT_S
        while True:
            await page.wait_for_timeout(1500)
            try:
                state = await self._doc.evaluate(_AFTER_SUBMIT_JS)
            except Exception:
                # The frame that held the form navigated away or was torn down —
                # the page itself says what happened now.
                await self._pick_frame()
                try:
                    state = await self._doc.evaluate(_AFTER_SUBMIT_JS)
                except Exception:
                    continue  # mid-navigation; look again
            if self._frame is not None:
                # A confirmation may be shown by the host page rather than inside the frame.
                try:
                    state["text"] = (state.get("text") or "") + "\n" + (
                        await page.evaluate("() => (document.body.innerText || '').slice(0, 20000)"))
                except Exception:
                    pass
            verdict = _submit_verdict(state, page.url != before)
            if verdict is not None:
                return verdict
            if time.monotonic() > deadline:
                return False, (f"the submission was still in progress after {SUBMIT_WAIT_S} seconds — it may yet "
                               "have gone through; check your email before retrying")

    async def upload(self, selector: str, file_path: str | Path, label: str = "") -> None:
        await self._upload_impl(selector, file_path, label)
        # Some sites clear the input's file list once they have read it;
        # what went in stays known here.
        self._uploaded.add(selector)
        if label:
            self._uploaded.add(label)

    async def _upload_impl(self, selector: str, file_path: str | Path, label: str = "") -> None:
        """Attach a file — the step no browser-tool MCP server can perform.

        Three ways in, tried in order. The input the scanner tagged, if it is
        still there. Failing that, any file input on the page (React forms
        re-render their upload widget, and the tagged element goes with it),
        preferring one whose label mentions the resume when there are several.
        Failing that, the native file chooser behind an "Upload" button, which
        is how Ashby and a few others build theirs: no input exists until the
        button is clicked, so the chooser is intercepted instead.
        """
        await self.start()
        p = Path(file_path).expanduser().resolve()
        if not p.is_file():
            raise FileNotFoundError(p)
        page = self._page
        doc = self._doc
        try:
            await doc.locator(selector).first.set_input_files(str(p), timeout=4000)
            await page.wait_for_timeout(700)
            return
        except Exception:
            pass

        inputs = doc.locator("input[type=file]")
        n = await inputs.count()
        if n:
            pick = inputs.first
            if n > 1:
                for i in range(n):
                    cand = inputs.nth(i)
                    near = (await cand.evaluate(
                        "e => ((e.labels && e.labels[0] && e.labels[0].innerText) || e.getAttribute('aria-label') || "
                        "(e.closest('label, div') || {}).innerText || '').toLowerCase()"
                    )) or ""
                    if re.search(r"resume|résumé|\bcv\b", near):
                        pick = cand
                        break
            await pick.set_input_files(str(p), timeout=4000)
            await page.wait_for_timeout(700)
            return

        want = re.compile(r"upload|attach|resume|résumé|\bcv\b|choose file|browse", re.I)
        button = doc.get_by_role("button", name=want).first
        if not await button.count():
            button = doc.get_by_text(want).first
        async with page.expect_file_chooser(timeout=5000) as fc:
            await button.click(timeout=5000)
        chooser = await fc.value
        await chooser.set_files(str(p))
        await page.wait_for_timeout(700)

    async def click(self, selector: str) -> None:
        await self.start()
        await self._doc.locator(selector).first.click()
        await self._page.wait_for_timeout(1200)

    async def advance(self, selector: str) -> None:
        """Click a Next/Continue control of a multi-step form and wait for
        the following step to render."""
        await self.start()
        baseline = await self._fields_present()
        try:
            await self._page.keyboard.press("Escape")  # any open menu would take this click instead
        except Exception:
            pass
        await self._doc.locator(selector).first.click()
        try:
            await self._page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        await self._page.wait_for_timeout(900)
        # The next step renders from JavaScript after the click (Oracle's
        # verification-code step arrives once its e-mail is sent, ten to
        # fifteen seconds later); wait for it, not for a blank.
        await self._wait_for_fields(20, baseline)
        await self._pick_frame()

    async def screenshot(self, path: str | Path) -> Path:
        await self.start()
        p = Path(path).expanduser().resolve()
        p.parent.mkdir(parents=True, exist_ok=True)
        await self._page.screenshot(path=str(p), full_page=True)
        return p

    async def unfilled_required(self, demanded: set[str] | None = None) -> list[dict]:
        """Required fields still empty — the check that runs before any submit.

        A radio group is one question: it is answered when any of its buttons
        is checked, so the unselected buttons are not "empty required fields".
        `demanded` names questions the page itself insisted on after a
        rejected submit; they count as required whatever the markup says."""
        fields = await self.describe_form()
        if demanded:
            low = {" ".join(d.split()).lower() for d in demanded if d}
            for f in fields:
                label = " ".join((f.get("label") or "").split()).lower().rstrip("*").strip()
                if label and any(d in label or label in d for d in low):
                    f["required"] = True

        boxes = [g for g in fields if g.get("type") == "checkbox"]

        def group_key(f: dict):
            if f.get("type") == "radio":
                return ("radio", f.get("group") or f.get("label"))
            if f.get("type") == "checkbox":
                # A list of choices, not a consent box: boxes sharing a name,
                # or (Ashby) sharing the question above them.
                if f.get("group") and sum(1 for g in boxes if g.get("group") == f["group"]) > 1:
                    return ("checkbox", f["group"])
                q = (f.get("section") or "", f.get("label") or "")
                if f.get("label") and f.get("option_label") and f["option_label"] != f["label"] and sum(
                        1 for g in boxes if (g.get("section") or "", g.get("label") or "") == q) > 1:
                    return ("checkbox-q", q)
            return None

        out, seen = [], set()
        for f in fields:
            key = group_key(f)
            if key:
                if key in seen:
                    continue
                seen.add(key)
                members = [g for g in fields if group_key(g) == key]
                if any(m.get("required") for m in members) and not any(m.get("checked") for m in members):
                    out.append(f)
            elif _is_empty(f) and f.get("selector") not in self._declined and not (
                    f.get("type") == "file" and (f.get("selector") in self._uploaded or (f.get("label") or "") in self._uploaded)):
                out.append(f)
        return out


# Phrases that show up on the pages this tool must never try to push through.
# Detection here is heuristic — it will miss some blockers and occasionally
# false-positive on a page that merely mentions "sign in" in a footer link —
# but a false skip costs one job, and a missed one means the run stalls on a
# page it cannot actually complete. Either way, this never attempts to solve
# what it finds; it names it and moves on.
_BLOCK_PHRASES = (
    "verify you are human", "i'm not a robot", "unusual traffic",
    "access denied", "are you a robot", "checking your browser",
)
# Phrases that also occur in ordinary posting text — "please verify all
# openings on our official careers page" is a scam warning, "protected by
# reCAPTCHA" a footer badge. They mean a wall only where there is no form.
_WEAK_BLOCK_PHRASES = ("captcha", "please verify", "security check")


_LOGIN_HOSTS = ("accounts.google.com", "login.microsoftonline.com", "login.live.com", "okta.com", "auth0.com",
                "login.salesforce.com", "signin.aws.amazon.com")


# A posting that is no longer there: not a wall to come back to, a record to close.
_GONE_PHRASES = ("page you are looking for doesn't exist", "page you're looking for doesn't exist", "job is no longer available",
                 "position is no longer available", "posting is no longer available", "no longer accepting applications",
                 "this job has been closed", "this position has been filled", "job has expired", "posting has expired",
                 "job you are looking for is no longer", "requisition is closed", "this job is not available")
_LOGIN_PATH = re.compile(r"/(signin|sign-in|login|log-in|auth|register|signup|sign-up|create-?account)(/|$)", re.I)
_SIGNIN_TEXT = re.compile(r"\b(sign in|log ?in|create (an )?account)\b", re.I)


_WD_ENTRY = re.compile(r"^(workExperience|education|language|socialNetworkAccounts)-(\d+)--(\w+?)(?:-dateSection(Month|Year|Day)-input)?$")


def _relabel_workday(fields: list[dict]) -> None:
    """Workday names its controls by entry and part in the DOM id
    (workExperience-6--startDate-dateSectionMonth-input) while the visible
    labels are just "Month" and "Year" under a "From*" caption, and the
    "I currently work here" box sits under the same caption. Give each such
    control the name a person reads: "From date — Month (MM)", in the
    section of its entry ("Work Experience 1")."""
    entry_section: dict[str, str] = {}
    for f in fields:
        m = _WD_ENTRY.match(f.get("dom_id") or "")
        if not m:
            continue
        entry = f"{m.group(1)}-{m.group(2)}"
        part, seg = m.group(3), m.group(4)
        if part in ("jobTitle", "companyName", "school", "schoolName") and f.get("section"):
            entry_section[entry] = f["section"]
    for f in fields:
        m = _WD_ENTRY.match(f.get("dom_id") or "")
        if not m:
            continue
        entry = f"{m.group(1)}-{m.group(2)}"
        part, seg = m.group(3), m.group(4)
        if seg:
            which = "From date" if part.startswith("start") else "To date" if part.startswith("end") else part
            f["label"] = f"{which} — {seg} ({'MM' if seg == 'Month' else 'YYYY' if seg == 'Year' else 'DD'})" + ("*" if part.startswith("start") else "")
            f["hint"] = "MM" if seg == "Month" else "YYYY" if seg == "Year" else "DD"
        elif part == "currentlyWorkHere":
            f["label"] = "I currently work here"
        elif part == "native" and f.get("type") == "checkbox":
            f["label"] = "I am fluent in this language"
        if entry in entry_section:
            f["section"] = entry_section[entry]
        elif m.group(1) == "workExperience":
            f["section"] = f"Work Experience {m.group(2)}"
        elif m.group(1) == "education":
            f["section"] = f"Education {m.group(2)}"


def _looks_like_application(fields: list[dict]) -> bool:
    """Whether a page's controls are an application, rather than the search
    box, job-alert signup or newsletter form a careers page keeps beside its
    Apply button. A file upload or a password settles it; otherwise a name
    with an email, or a longer form asking application things."""
    if not fields:
        return False
    types = {(f.get("type") or "").lower() for f in fields}
    if "file" in types or "password" in types:
        return True
    labels = " | ".join((f.get("label") or f.get("name") or "").lower() for f in fields)
    has_name = re.search(r"\bname\b", labels) is not None
    has_email = "email" in types or "email" in labels
    app_words = re.search(r"phone|resume|résumé|linkedin|school|university|degree|authori[sz]|sponsor|gpa|graduat|cover letter", labels)
    return bool((has_name and has_email) or (len(fields) >= 6 and app_words))


def blocker_verdict(fields: list[dict], text: str, url: str = "", after_apply: bool = False) -> str | None:
    """The page's kind, from its controls and visible text (see detect_blocker).
    `after_apply` means the Apply control has been clicked: whatever fields
    are here are the form's own first step, and a page offering only
    sign-in routes is the portal's account wall."""
    text = text.lower()
    if any(p in text for p in _BLOCK_PHRASES):
        return "bot_check"
    # An identity provider's sign-in page — Google's careers site sends
    # applicants to accounts.google.com — shows one field at a time, so the
    # password test below never fires; the address says what it is.
    host = (urlsplit(url).netloc or "").lower()
    path = (urlsplit(url).path or "").lower()
    if url and len(fields) <= 3 and (any(host == h or host.endswith("." + h) for h in _LOGIN_HOSTS)
                                     or _LOGIN_PATH.search(path)):
        return "login_required"
    if len(fields) < 3 and any(p in text for p in _WEAK_BLOCK_PHRASES):
        return "bot_check"
    if any(f.get("type") == "password" for f in fields) and not any(f.get("type") == "file" for f in fields):
        return "login_required"
    if not fields or (not after_apply and not _looks_like_application(fields)):
        if any(p in text for p in _GONE_PHRASES):
            return "posting_gone"
        if after_apply and len(fields) <= 3 and _SIGNIN_TEXT.search(text):
            return "login_required"
        return "no_form_found"
    return None


async def detect_blocker(session: "ApplySession", after_apply: bool = False) -> str | None:
    """None means the page looks like a normal, fillable form.

    Checked in order: an unmistakable bot/CAPTCHA phrase in the visible text
    is a wall; a phrase that ordinary postings also use counts only when the
    page has no form to speak of. A page with a password field and no file
    input is a login wall, not an application (a real apply form sometimes
    also has a "create an account" checkbox with a password field alongside
    the resume upload — the file-input check is what tells those apart). No
    fields at all means the page has not finished loading, is JS-rendered
    oddly, or is not an application page.
    """
    fields = await session.describe_form()
    text = await session.read_text()
    try:
        url = session._page.url
    except Exception:
        url = ""
    return blocker_verdict(fields, text, url, after_apply)
