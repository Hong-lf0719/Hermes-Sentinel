// 公司/岗位名启发式提取（popup 与 background 共用）
// v2: 过滤非公司标题 + URL域名反推 + 增强岗位匹配

// ── 常见非公司页面标题（命中则丢弃，不当作公司名）──
var _NON_COMPANY = /^(?:我的|个人|应聘|申请|投递|简历|登录|注册|首页|招聘|职位|搜索|详情|确认|成功|完成|提交|欢迎|中心|后台|管理|系统|通知|消息|设置|帮助|关于|协议|隐私|用户|会员|账户|订单|购物车|收藏|历史|足迹)$/i;

// ── 域名 → 公司名映射（常见校招/社招官网）──
var _DOMAIN_COMPANY = {
  // 大厂
  "tencent": "腾讯", "qq": "腾讯", "wechat": "腾讯(微信)",
  "alibaba": "阿里巴巴", "aliyun": "阿里云", "taobao": "淘宝", "tmall": "天猫", "dingtalk": "钉钉",
  "bytedance": "字节跳动", "toutiao": "字节跳动", "douyin": "抖音", "lipushanshi": "字节跳动",
  "huawei": "华为", "baidu": "百度", "jd": "京东", "meituan": "美团", "dianping": "美团点评",
  "pinduoduo": "拼多多", "xiaomi": "小米", "oppo": "OPPO", "vivo": "VIVO",
  "netease": "网易", "163": "网易", "youdao": "有道",
  "suning": "苏宁", "360": "奇虎360", "qihoo": "360",
  "lenovo": "联想", "haier": "海尔", "midea": "美的", "gree": "格力",
  "dji": "大疆", "sensetime": "商汤", "cambricon": "寒武纪", "horizon": "地平线",
  "zhipu": "智谱AI", "minimax": "MiniMax", "moonshot": "Moonshot(Kimi)",
  "deepseek": "DeepSeek", "baichuan": "百川智能", "01ai": "零一万物",
  "antgroup": "蚂蚁集团", "antfin": "蚂蚁金服",
  "didichuxing": "滴滴", "hellobike": "哈啰出行",
  "kuaishou": "快手", "bilibili": "哔哩哔哩",
  "xiaohongshu": "小红书", "red": "小红书",
  "shein": "SHEIN", "ctrip": "携程", "ly": "同程旅行",
  "changfei": "长飞光纤", "zfyt": "中飞",
  "zte": "中兴通讯", "fiberhome": "烽火通信", "yonyou": "用友网络",
  "kingdee": "金蝶软件", "iFlytek": "科大讯飞", "iflytek": "科大讯飞",
  "inspur": "浪潮信息", "huice": "慧策", "beisen": "北森",
  "wps": "金山办公", "kingsoft": "金山软件", "wondershare": "万兴科技",
  "smartisan": "坚果", "nubia": "努比亚", "realme": "真我",
  "chinaunicom": "中国联通", "chinamobile": "中国移动", "chinatelecom": "中国电信",
  "icbc": "工商银行", "ccb": "建设银行", "abc": "农业银行", "boc": "中国银行",
  "cmb": "招商银行", "spdb": "浦发银行", "citic": "中信银行",
  // 招聘平台（不反推，标记为平台）
  "zhaopin": null, "51job": null, "liepin": null, "bosszhipin": null, "boss": null,
  "lagou": null, "kanzhun": null, "maimai": null,
  "nowcoder": null, "haitou": null, "yingjiesheng": null, "iguopin": null,
};

// ── 门户/平台类噪音后缀（"联影网申门户" 应归一为 "联影"）──
var _PORTAL_SUFFIX = [
  "网申门户", "网申系统", "网申平台", "招聘门户", "投递门户", "门户系统", "门户",
  "校招官网", "校招网", "招聘官网", "招聘网", "投递系统", "个人中心", "申请中心",
  "我的投递", "投递记录", "申请记录", "网申记录", "招聘首页", "校园招聘"
];

// 把抓取到的公司名归一：去【】季节前缀、去门户/平台后缀、去前导噪音。
// 抓取与匹配两端共用同一口径，才能保证"同步"。
function normalizeCompany(s) {
  if (!s) return "";
  s = String(s).trim();
  s = s.replace(/^【[^】]{0,20}】/, "");                 // 【2026校招】
  s = s.replace(/^(?:秋招|春招|校招|社招|实习)[-_－\s]*/, "");
  for (var i = 0; i < _PORTAL_SUFFIX.length; i++) {
    var suf = _PORTAL_SUFFIX[i];
    if (s.length > suf.length && s.slice(s.length - suf.length) === suf) {
      s = s.slice(0, s.length - suf.length);
      break;
    }
  }
  s = s.replace(/^(?:我的|个人)[-_－\s]*/, "");
  s = s.replace(/^[·\-_\s]+|[·\-_\s]+$/g, "").trim();
  return s;
}

function guessCompany(input, url) {
  if (!input) return "";

  // ① 现有正则：后缀词匹配
  var m = input.match(/([一-龥A-Za-z·&]{2,20}?)(?:公司|集团|科技|网络技术|软件|信息技术|股份|有限|大学)/);
  if (m) {
    var c = normalizeCompany(m[1].replace(/[·\s]+$/, ""));
    if (c && !_NON_COMPANY.test(c)) return c;
  }

  // ② 冒号前缀模式（"腾讯 - 招聘" → 腾讯）
  m = input.match(/^([一-龥A-Za-z·&]{2,16})[_\-－\s:：]/);
  if (m) {
    var c2 = normalizeCompany(m[1]);
    if (c2 && !_NON_COMPANY.test(c2)) return c2;
  }

  // ③ URL 域名反推（最后兜底）
  if (url) {
    try {
      var host = new URL(url).hostname.toLowerCase();
      for (var d in _DOMAIN_COMPANY) {
        var comp = _DOMAIN_COMPANY[d];
        if (comp === null) continue;
        // 严格域段匹配：键必须命中完整域名段（开头/独立段），
        // 裸子串会让 "ly" 命中 app**ly**.mokahr.com、"ke" 命中任意含 ke 的域名
        if (host === d || host.indexOf(d + ".") === 0 || host.indexOf("." + d + ".") !== -1)
          return comp;
      }
    } catch (e) {}
  }

  // ④ 截断兜底（但过滤掉明显非公司的）
  var fallback = normalizeCompany(input.slice(0, 16).trim());
  return _NON_COMPANY.test(fallback) ? "" : fallback;
}

function guessTitle(text, docTitle, candidates) {
  // 1) 候选池：文档标题 + 页面结构化线索 + 正文标签跟随
  var pool = [];
  function add(s) {
    if (!s) return;
    s = s.replace(/\s+/g, " ").trim();
    if (s.length >= 2 && s.length <= 60) pool.push(s);
  }
  if (docTitle) {
    add(docTitle);
    docTitle.split(/[-_|–—/·:：\s]+/).forEach(add);
  }
  if (candidates) {
    if (typeof candidates.forEach === "function") candidates.forEach(add);
    else if (typeof candidates === "string") add(candidates);
  }
  if (text) {
    var lr = /(应聘职位|申请职位|应聘岗位|申请岗位|职位名称|岗位名称|招聘职位|投递职位|报考职位|应聘方向|意向岗位|求职意向|岗位)[：:\s]*([^\n：:，,。；;]{2,30})/g;
    var m;
    while ((m = lr.exec(text)) !== null) add(m[2]);
  }

  // 2) 从候选里挑最像"岗位名"的（强岗位词加权，过长/过短/非岗位词降权）
  var JOB_HINT = /(工程师|开发|分析师|管培|运营|产品|测试|算法|研发|实习|专员|经理|架构师|设计师|专家|岗|职位|助理|顾问|技术|销售|市场|财务|人力|支持|培训|项目|数据|前端|后端|全栈|移动|嵌入|硬件|芯片|运维|DBA|DevOps|SRE|QA|Java|Python|Go)/i;
  var STRONG = /(工程师|分析师|管培生|架构师|设计师|专员|经理|顾问|专家|助理|开发|运营|产品|测试|算法|研发)/;
  var NON_JOB = /^(网申投递|网申|投递|申请|记录|公司介绍|招聘类型|我的应聘|申请记录|投递记录|提交成功|报名成功|简历|登录|注册|首页|个人中心|我的|成功|完成|确认)$/i;
  var best = "", bestScore = -1;
  for (var i = 0; i < pool.length; i++) {
    var c = cleanTitle(pool[i]);
    if (!c || NON_JOB.test(c)) continue;
    var score = 0;
    if (JOB_HINT.test(c)) score += 10;
    if (STRONG.test(c)) score += 5;
    score -= Math.abs(c.length - 8);
    if (score > bestScore) { bestScore = score; best = c; }
  }
  if (best && bestScore >= 0) return best;

  // 3) 兜底：正文后缀匹配（不依赖候选）
  if (text) {
    text = text.replace(/\s+/g, " ");
    var suffixes = [
      "开发工程师", "软件开发工程师", "大数据平台开发工程师", "前端开发工程师", "后端开发工程师",
      "全栈工程师", "算法工程师", "机器学习工程师", "深度学习工程师", "NLP工程师",
      "数据分析师", "数据科学家", "数据开发工程师", "数据仓库工程师", "BI工程师",
      "产品经理", "产品运营", "用户运营", "内容运营", "增长运营",
      "测试工程师", "自动化测试", "测试开发", "QA工程师",
      "运维工程师", "DevOps工程师", "SRE工程师", "DBA",
      "嵌入式工程师", "硬件工程师", "芯片设计", "IC设计",
      "管培生", "管理培训生", "储备干部",
      "解决方案架构师", "技术支持工程师", "售前工程师",
      "数字化与数据开发工程师", "数字化专员", "数字化转型顾问",
      "Java开发", "Python开发", "Go开发", "C++开发", "前端开发", "后端开发",
      "Android开发", "iOS开发", "移动端开发",
      "视觉设计师", "UI设计师", "UX设计师", "交互设计师",
      "人力资源专员", "HRBP", "财务分析师", "审计专员"
    ];
    var best2 = "";
    for (var j = 0; j < suffixes.length; j++) {
      var idx = text.indexOf(suffixes[j]);
      if (idx !== -1) {
        var start = Math.max(0, idx - 20);
        var chunk = text.slice(start, idx + suffixes[j].length);
        var cand = cleanTitle(chunk);
        if (cand.length > best2.length) best2 = cand;
      }
    }
    if (best2) return best2;

    var m2 = text.match(/([一-龥A-Za-z·]{2,16}?(?:工程师|开发|分析师|管培|运营|产品|测试|算法|研发|实习生|专员|经理|总监|架构师|设计师|专家))/);
    if (m2) return cleanTitle(m2[1]);
  }
  return "";
}

// 清理岗位名：去掉前后噪音
function cleanTitle(s) {
  s = s.replace(/^[^\u4e00-\u9fa5A-Za-z]*/, "");   // 去开头非中文/字母
  s = s.replace(/[^\u4e00-\u9fa5A-Za-z]*$/g, "");     // 去结尾非中文/字母
  s = s.replace(/\s{2,}/g, " ").trim();
  return s.length > 1 ? s : "";
}
