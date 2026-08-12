// 公司/岗位名启发式提取（popup 与 background 共用）
// v2: 过滤非公司标题 + URL域名反推 + 增强岗位匹配

// ── 常见非公司页面标题（命中则丢弃，不当作公司名）──
var _NON_COMPANY = /^(?:我的|个人|应聘|申请|投递|简历|登录|注册|首页|招聘|职位|搜索|详情|确认|成功|完成|提交|欢迎|中心|后台|管理|系统|通知|消息|设置|帮助|关于|协议|隐私|用户|会员|账户|订单|购物车|收藏|历史|足迹|足迹|足迹)$/i;

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
  "kuaishou": "快手", "bilibili": "哔哩哔哩", "bilibili": "B站",
  "xiaohongshu": "小红书", "red": "小红书",
  "shein": "SHEIN", "ctrip": "携程", "ctrip": "携程", "ly": "同程旅行",
  "ke": "科恩", "changfei": "长飞光纤", "zfyt": "中飞",
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
};

function guessCompany(input, url) {
  if (!input) return "";

  // ① 现有正则：后缀词匹配
  var m = input.match(/([一-龥A-Za-z·&]{2,20}?)(?:公司|集团|科技|网络技术|软件|信息技术|股份|有限|大学)/);
  if (m) {
    var c = m[1].replace(/[·\s]+$/, "");
    if (!_NON_COMPANY.test(c)) return c;
  }

  // ② 冒号前缀模式（"腾讯 - 招聘" → 腾讯）
  m = input.match(/^([一-龥A-Za-z·&]{2,16})[_\-－\s:：]/);
  if (m && !_NON_COMPANY.test(m[1])) return m[1];

  // ③ URL 域名反推（最后兜底）
  if (url) {
    try {
      var host = new URL(url).hostname.toLowerCase();
      // 先精确匹配
      for (var d in _DOMAIN_COMPANY) {
        if (_DOMAIN_COMPANY[d] !== null && host.indexOf(d) !== -1)
          return _DOMAIN_COMPANY[d];
      }
    } catch(e) {}
  }

  // ④ 截断兜底（但过滤掉明显非公司的）
  var fallback = input.slice(0, 16).trim();
  return _NON_COMPANY.test(fallback) ? "" : fallback;
}

function guessTitle(text) {
  if (!text) return "";
  text = text.replace(/\s+/g, " ");

  // ① 明确的岗位标签
  var m = text.match(/(?:应聘|申请|投递|报名|报考|职位|岗位)[为：:\s]*([一-龥A-Za-z·]{2,30}?)(?:\s|$|。|，|；|\(|（)/);
  if (m) return cleanTitle(m[1]);

  // ② 常见岗位后缀（从文本中找最长匹配）
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
  var best = "";
  for (var i = 0; i < suffixes.length; i++) {
    var idx = text.indexOf(suffixes[i]);
    if (idx !== -1) {
      // 往前抓修饰词
      var start = Math.max(0, idx - 20);
      var chunk = text.slice(start, idx + suffixes[i].length);
      var cand = cleanTitle(chunk);
      if (cand.length > best.length) best = cand;
    }
  }
  if (best) return best;

  // ③ 宽泛后缀兜底
  m = text.match(/([一-龥A-Za-z·]{2,16}?(?:工程师|开发|分析师|管培|运营|产品|测试|算法|研发|实习生|专员|经理|总监|架构师|设计师|专家))/);
  if (m) return cleanTitle(m[1]);

  return "";
}

// 清理岗位名：去掉前后噪音
function cleanTitle(s) {
  s = s.replace(/^[^\u4e00-\u9fa5A-Za-z]*/, "");   // 去开头非中文/字母
  s = s.replace(/[^\u4e00-\u9fa5A-Za-z]*$/g, "");     // 去结尾非中文/字母
  s = s.replace(/\s{2,}/g, " ").trim();
  return s.length > 1 ? s : "";
}
