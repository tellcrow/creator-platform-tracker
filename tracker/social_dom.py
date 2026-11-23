"""Read only rendered DOM on creator list pages, with no private API/state access.

Selectors are based on the signed-in creator pages inspected on 2026-10-26.
All counters come from the content-management cards, never detail pages.
"""

_LOGIN_PROMPT_JS = r"""
  const hasLoginPrompt = () => {
    // Creator captions, names and signatures are authored content, not login
    // controls. Inspect visible text outside those regions and skip ancestors
    // containing them, so body text cannot turn a quoted login phrase into an
    // authentication failure. A separate login dialog/alert remains detectable.
    const excluded='.note-card,[class*="video-card-content-"],.user-info,' +
      '[class^="header-"]:has([class^="unique_id-"])';
    const pattern=/扫码登录|短信登录|手机号登录|请先登录|登录已过期|登录失效/;
    return Array.from(document.querySelectorAll('button,[role="button"],[role="dialog"],[role="alert"],div,p,span'))
      .some(e => e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden' &&
        !e.closest(excluded) && !e.querySelector(excluded) &&
        pattern.test((e.innerText || e.textContent || '').replace(/\s+/g,' ').trim()));
  };
"""

SNAPSHOT_JS = r"""(platform) => {
""" + _LOGIN_PROMPT_JS + r"""
  const text = e => e ? (e.innerText || e.textContent || '').replace(/\s+/g, ' ').trim() : '';
  const shown = e => !!e && e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden';
  const all = (s, root=document) => Array.from(root.querySelectorAll(s));
  const shownText = s => all(s).filter(shown).map(text).filter(Boolean);
  const bodyText = text(document.body);
  const inputs = all('input').filter(shown).filter(e => !['checkbox','radio','button','submit','hidden'].includes(e.type));
  const hasFilterInput = inputs.some(e => String(e.value || '').trim());
  let nodes=[], cards=[], accountNames=[], total=null, filtersDefault=false;
  const count = s => { const m=s.match(/(?:全部|作品)\s*[（(]?\s*(\d+)\s*[）)]?/); return m ? Number(m[1]) : null; };
  if (platform === 'xiaohongshu') {
    nodes = all('.note-card').filter(shown);
    accountNames = shownText('.user-info .name-box');
    const active = shownText('.tab-item--active');
    total = active.length === 1 ? count(active[0]) : null;
    filtersDefault = active.length === 1 && /^全部\s*[（(]?\s*\d+\s*[）)]?$/.test(active[0]) && !hasFilterInput;
    cards = nodes.map(card => {
      let stableId='';
      try {
        const observed = JSON.parse(card.getAttribute('data-impression') || '{}');
        const candidate = observed.noteTarget && observed.noteTarget.value && observed.noteTarget.value.noteId;
        if (typeof candidate === 'string' && /^[a-fA-F0-9]{24}$/.test(candidate)) stableId=candidate;
      } catch (_) { /* malformed DOM metadata is never an identity */ }
      const titleEl = card.querySelector('.note-card__title');
      const title = text(titleEl);
      const stats = all('.note-card__stat span',card).map(text);
      const metrics={};
      if (stats.length === 5) ['views','comments','likes','favorites','shares'].forEach((label,i)=>metrics[label]=stats[i]);
      return {title, title_truncated:/(?:\.\.\.|…)\s*$/.test(title),
        publish_time:text(card.querySelector('.note-card__row--time')),
        stable_id:stableId, metrics};
    });
  } else if (platform === 'douyin') {
    const contents=all('[class*="video-card-content-"]').filter(shown);
    const parents=contents.map(content => {
      let node=content.parentElement;
      while (node && node!==document.body) {
        if (Array.from(node.classList).some(c=>/^video-card-(?!content-)/.test(c))) return node;
        node=node.parentElement;
      }
      return content;
    });
    nodes=Array.from(new Set(parents));
    const countMatch=bodyText.match(/作品\s*[（(]?\s*(\d+)\s*[）)]?/);
    total=countMatch ? Number(countMatch[1]) : null;
    // Check the actual controls, not unrelated occurrences of these labels in
    // the body. Nonempty search/date inputs also invalidate an all-content run.
    const defaults = [
      ['[role="combobox"][class*="manage-v2-status-select-"]','审核状态'],
      ['[class*="manage-v2-genre-select-"]','体裁'],
      ['[role="combobox"].dy-creator-content-datepicker-input button[class*="manage-v2-date-button-"]','发布日期'],
      ['[class*="manage-v2-sort-select-"]','最新发布']
    ];
    filtersDefault=total!==null && !hasFilterInput && defaults.every(([selector,label])=>{
      const entries=all(selector).filter(shown); return entries.length===1 && text(entries[0])===label;
    });
    cards=nodes.map(card => {
      const title=text(card.querySelector('[class*="info-title-text-"]'));
      const metrics={};
      all('[class*="metric-item-container-"]',card).forEach(metric=>{
        const label=text(metric.querySelector('[class*="metric-label-"]'));
        const value=text(metric.querySelector('[class*="metric-value-"]'));
        if (label) metrics[label]=value;
      });
      return {title,title_truncated:/(?:\.\.\.|…)\s*$/.test(title),
        publish_time:text(card.querySelector('[class*="info-op-"]')),
        metrics,visibility:/私密/.test(text(card)) ? 'private' : 'public',
        content_type:Object.hasOwn(metrics,'平均浏览图片数') ? 'images' :
          (Object.hasOwn(metrics,'完播率') ? 'video' : '')};
    });
  }
  let scrollRoot=document.scrollingElement || document.documentElement;
  if (nodes.length) {
    let ancestor=nodes[0].parentElement;
    while(ancestor && ancestor!==document.body) {
      const style=getComputedStyle(ancestor);
      if (/(auto|scroll)/.test(style.overflowY) && ancestor.scrollHeight>ancestor.clientHeight+2) {
        scrollRoot=ancestor; break;
      }
      ancestor=ancestor.parentElement;
    }
  }
  const loading=all('[aria-busy="true"], [role="progressbar"], .loading, .loading-container, .el-loading-mask').some(shown);
  // A freshly navigated page can transiently say "全部 0" before loading. Zero
  // alone is never evidence of an empty account. Require an explicit visible
  // empty-state message as well; otherwise the collector keeps waiting.
  const emptyTexts=new Set(['暂无笔记','暂无作品','还没有发布作品','还没有发布过笔记']);
  const emptyConfirmed=nodes.length===0 && all('div,p,span').some(e=>shown(e) && emptyTexts.has(text(e)));
  return {cards,account_names:accountNames,total,filters_default:filtersDefault,
    at_bottom:scrollRoot.scrollTop+scrollRoot.clientHeight>=scrollRoot.scrollHeight-4,
    end_reached:/没有更多作品|没有更多了|暂无更多/.test(bodyText),
    login_required:hasLoginPrompt(),
    loading,empty_confirmed:emptyConfirmed};
}"""

SCROLL_JS = r"""(platform) => {
  const selector=platform==='xiaohongshu' ? '.note-card' : '[class*="video-card-content-"]';
  const node=document.querySelector(selector);
  let root=document.scrollingElement || document.documentElement;
  let ancestor=node && node.parentElement;
  while(ancestor && ancestor!==document.body) {
    if (/(auto|scroll)/.test(getComputedStyle(ancestor).overflowY) && ancestor.scrollHeight>ancestor.clientHeight+2) {
      root=ancestor; break;
    }
    ancestor=ancestor.parentElement;
  }
  const before=root.scrollTop;
  root.scrollBy({top:Math.max(200,Math.floor(root.clientHeight*0.8)),behavior:'instant'});
  return root.scrollTop!==before;
}"""

ACCOUNT_JS = r"""(platform) => {
""" + _LOGIN_PROMPT_JS + r"""
  const visible=e=>e.getClientRects().length && getComputedStyle(e).visibility!=='hidden';
  const text=e=>(e.innerText || e.textContent || '').replace(/\s+/g,' ').trim();
  const selector=platform==='xiaohongshu' ? '.user-info .name-box' :
    '[class^="header-"]:has([class^="unique_id-"]) [class^="name-"]';
  const names=Array.from(document.querySelectorAll(selector)).filter(visible).map(text).filter(Boolean);
  const ids=platform==='douyin' ? Array.from(document.querySelectorAll(
    '[class^="header-"] [class^="unique_id-"]')).filter(visible).map(e=>{
      const m=text(e).match(/^抖音号\s*[：:]\s*(\S+)$/); return m ? m[1] : '';
    }).filter(Boolean) : [];
  return {names,ids,login_required:hasLoginPrompt()};
}"""
