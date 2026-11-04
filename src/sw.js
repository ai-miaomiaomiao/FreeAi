// Service worker —— 存在的**唯一**目的是让 Chrome 认为这个站"可安装"。
//
// 它**故意不缓存任何东西**：
//
//   一旦开始缓存，你改完 index.html 刷新看到的还是旧的，而这类问题特别难认
//   —— Ctrl+F5 对 service worker 管理的请求不起作用，界面上又没有任何提示，
//   最后只能怀疑"我刚改的代码是不是没生效"。
//
//   这个工具是本地起的、页面只从 127.0.0.1 拿，本来就没有"离线可用"的需求，
//   拿缓存换来的那点加载速度不值得这个代价。
//
// 所以下面只有一个空 fetch 处理器：请求照常走网络，不拦、不存、不回退。

self.addEventListener("install", () => {
  // 不等待旧的 worker 退出，装上就生效
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  // 立刻接管已经开着的页面，免得要关掉重开
  event.waitUntil(self.clients.claim());
});

self.addEventListener("fetch", () => {
  // 什么都不做 —— 浏览器会按正常流程去请求网络
});
