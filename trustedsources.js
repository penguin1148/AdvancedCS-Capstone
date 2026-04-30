// Top 5 reliable / reputable news sources by country
// Use this in script.js
//
// `trustedSources` lists the human-readable source names; the parallel
// `trustedSourceDomains` map below holds the publisher domains we use to
// filter GDELT results (which expose a `domain` field, not a source name).

const trustedSources = {
  US: [
    "Reuters",
    "Associated Press",
    "NPR",
    "PBS NewsHour",
    "Wall Street Journal"
  ],

  IN: [
    "The Hindu",
    "Indian Express",
    "Reuters",
    "BBC News",
    "NDTV"
  ],

  GB: [
    "BBC News",
    "Reuters",
    "Financial Times",
    "The Guardian",
    "Sky News"
  ],

  CA: [
    "CBC News",
    "Reuters",
    "The Globe and Mail",
    "CTV News",
    "National Post"
  ],

  AU: [
    "ABC News",
    "Reuters",
    "The Sydney Morning Herald",
    "SBS News",
    "The Australian"
  ],

  FR: [
    "France 24",
    "Reuters",
    "Le Monde",
    "AFP",
    "Euronews"
  ],

  DE: [
    "DW",
    "Reuters",
    "Der Spiegel",
    "Frankfurter Allgemeine",
    "Tagesschau"
  ],

  JP: [
    "NHK",
    "Reuters",
    "Japan Times",
    "Kyodo News",
    "Nikkei Asia"
  ],

  BR: [
    "Reuters",
    "Folha de S.Paulo",
    "O Globo",
    "Estadão",
    "UOL Notícias"
  ],

  MX: [
    "Reuters",
    "El Universal",
    "Milenio",
    "La Jornada",
    "Animal Político"
  ],

  ES: [
    "Reuters",
    "El País",
    "RTVE",
    "ABC",
    "La Vanguardia"
  ],

  IT: [
    "Reuters",
    "ANSA",
    "Corriere della Sera",
    "La Repubblica",
    "RAI News"
  ],

  KR: [
    "Yonhap News",
    "Reuters",
    "Korea Herald",
    "KBS News",
    "JoongAng Daily"
  ],

  CN: [
    "Reuters",
    "Caixin",
    "South China Morning Post",
    "Xinhua",
    "China Daily"
  ],

  RU: [
    "Reuters",
    "Meduza",
    "TASS",
    "Novaya Gazeta",
    "The Moscow Times"
  ],

  ZA: [
    "Reuters",
    "News24",
    "Mail & Guardian",
    "Business Day",
    "SABC News"
  ],

  NG: [
    "Reuters",
    "Punch",
    "Premium Times",
    "Channels TV",
    "The Guardian Nigeria"
  ],

  PK: [
    "Reuters",
    "Dawn",
    "The News International",
    "Geo News",
    "Express Tribune"
  ],

  SG: [
    "Reuters",
    "The Straits Times",
    "Channel NewsAsia",
    "Today Online",
    "Business Times"
  ],

  AE: [
    "Reuters",
    "The National",
    "Gulf News",
    "Khaleej Times",
    "Arab News"
  ]
};

// Publisher domains for each trusted source, keyed by ISO 3166 alpha-2 code.
// A story counts as trusted if its GDELT `domain` equals one of these or is a
// subdomain of one (e.g. `www3.nhk.or.jp` matches `nhk.or.jp`). Order mirrors
// `trustedSources` above for easy auditing.
const trustedSourceDomains = {
  US: [
    "reuters.com",
    "apnews.com", "ap.org",
    "npr.org",
    "pbs.org",
    "wsj.com"
  ],

  IN: [
    "thehindu.com",
    "indianexpress.com",
    "reuters.com",
    "bbc.com", "bbc.co.uk",
    "ndtv.com"
  ],

  GB: [
    "bbc.com", "bbc.co.uk",
    "reuters.com",
    "ft.com",
    "theguardian.com",
    "news.sky.com", "sky.com"
  ],

  CA: [
    "cbc.ca",
    "reuters.com",
    "theglobeandmail.com",
    "ctvnews.ca",
    "nationalpost.com"
  ],

  AU: [
    "abc.net.au",
    "reuters.com",
    "smh.com.au",
    "sbs.com.au",
    "theaustralian.com.au"
  ],

  FR: [
    "france24.com",
    "reuters.com",
    "lemonde.fr",
    "afp.com",
    "euronews.com"
  ],

  DE: [
    "dw.com",
    "reuters.com",
    "spiegel.de",
    "faz.net",
    "tagesschau.de"
  ],

  JP: [
    "nhk.or.jp",
    "reuters.com",
    "japantimes.co.jp",
    "kyodonews.net",
    "asia.nikkei.com"
  ],

  BR: [
    "reuters.com",
    "folha.uol.com.br",
    "oglobo.globo.com", "globo.com",
    "estadao.com.br",
    "uol.com.br"
  ],

  MX: [
    "reuters.com",
    "eluniversal.com.mx",
    "milenio.com",
    "jornada.com.mx",
    "animalpolitico.com"
  ],

  ES: [
    "reuters.com",
    "elpais.com",
    "rtve.es",
    "abc.es",
    "lavanguardia.com"
  ],

  IT: [
    "reuters.com",
    "ansa.it",
    "corriere.it",
    "repubblica.it",
    "rainews.it"
  ],

  KR: [
    "yna.co.kr",
    "reuters.com",
    "koreaherald.com",
    "kbs.co.kr",
    "koreajoongangdaily.joins.com"
  ],

  CN: [
    "reuters.com",
    "caixinglobal.com", "caixin.com",
    "scmp.com",
    "xinhuanet.com", "news.cn",
    "chinadaily.com.cn"
  ],

  RU: [
    "reuters.com",
    "meduza.io",
    "tass.com", "tass.ru",
    "novayagazeta.eu", "novayagazeta.ru",
    "themoscowtimes.com"
  ],

  ZA: [
    "reuters.com",
    "news24.com",
    "mg.co.za",
    "businesslive.co.za",
    "sabcnews.com"
  ],

  NG: [
    "reuters.com",
    "punchng.com",
    "premiumtimesng.com",
    "channelstv.com",
    "guardian.ng"
  ],

  PK: [
    "reuters.com",
    "dawn.com",
    "thenews.com.pk",
    "geo.tv",
    "tribune.com.pk"
  ],

  SG: [
    "reuters.com",
    "straitstimes.com",
    "channelnewsasia.com",
    "todayonline.com",
    "businesstimes.com.sg"
  ],

  AE: [
    "reuters.com",
    "thenationalnews.com",
    "gulfnews.com",
    "khaleejtimes.com",
    "arabnews.com"
  ]
};
