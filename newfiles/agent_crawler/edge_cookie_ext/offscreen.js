// 这个页面只干两件事：替后台生成 blob 下载地址、下载完把地址撤销。
// Service Worker（后台）里没有 URL.createObjectURL / revokeObjectURL，只有页面环境才有，
// 所以要下载导出的 JSON（并且下载完不泄漏），必须借这个同源的隐藏页面。
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg) return;

  if (msg.type === 'make-blob-url') {
    try {
      const blob = new Blob([msg.text], { type: 'application/json' });
      sendResponse({ url: URL.createObjectURL(blob) });
    } catch (e) {
      sendResponse({ error: (e && e.message) ? e.message : String(e) });
    }
    return true;
  }

  if (msg.type === 'revoke-blob-url') {
    try {
      URL.revokeObjectURL(msg.url);
      sendResponse({ ok: true });
    } catch (e) {
      sendResponse({ error: (e && e.message) ? e.message : String(e) });
    }
    return true;
  }
});
