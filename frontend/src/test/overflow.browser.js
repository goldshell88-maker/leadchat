(async () => {
  await new Promise(r => setTimeout(r, 1800));
  const W = document.documentElement.clientWidth;

  // Есть ли предок, который прокручивается по горизонтали: тогда «вылезание»
  // за окно — это нормальная прокрутка, а не поломка вёрстки.
  const insideScroller = (el) => {
    for (let p = el.parentElement; p; p = p.parentElement) {
      const ox = getComputedStyle(p).overflowX;
      if (ox === 'auto' || ox === 'scroll' || ox === 'hidden') return true;
    }
    return false;
  };

  const out = [];
  document.querySelectorAll('*').forEach((el) => {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') return;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return;
    if (insideScroller(el)) return;

    // Содержимое не помещается, и прокрутки нет — текст обрезан или налезает.
    const clipped = el.scrollWidth - el.clientWidth > 2
      && cs.overflowX === 'visible' && el.children.length === 0;
    const outside = r.right > W + 2 || r.left < -2;

    if (clipped || outside) {
      const cls = (typeof el.className === 'string' ? el.className : '').split(' ')[0] || el.tagName;
      out.push({ el: cls, txt: (el.textContent || '').trim().slice(0, 32),
                 обрезано: clipped ? el.scrollWidth - el.clientWidth : 0,
                 заОкно: outside ? Math.round(r.right - W) : 0 });
    }
  });

  const seen = new Set();
  const uniq = out.filter(o => { const k = o.el + o.обрезано + o.заОкно;
    if (seen.has(k)) return false; seen.add(k); return true; });

  return JSON.stringify({
    окно: W, страница: location.pathname,
    горизонтальныйСкроллСтраницы: document.documentElement.scrollWidth > W + 2,
    найдено: uniq.length, примеры: uniq.slice(0, 8),
  }, null, 1);
})()
