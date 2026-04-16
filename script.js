const countryNames = {
  "AF": "Afghanistan", "AL": "Albania", "DZ": "Algeria", "AI": "Anguilla",
  "AM": "Armenia", "AW": "Aruba", "AT": "Austria", "BH": "Bahrain",
  "BD": "Bangladesh", "BB": "Barbados", "BY": "Belarus", "BE": "Belgium",
  "BZ": "Belize", "BJ": "Benin", "BM": "Bermuda", "BT": "Bhutan",
  "BO": "Bolivia", "BA": "Bosnia and Herzegovina", "BW": "Botswana", "BR": "Brazil",
  "VG": "British Virgin Islands", "BN": "Brunei Darussalam", "BG": "Bulgaria",
  "BF": "Burkina Faso", "BI": "Burundi", "KH": "Cambodia", "CM": "Cameroon",
  "CF": "Central African Republic", "TD": "Chad", "CO": "Colombia",
  "CR": "Costa Rica", "HR": "Croatia", "CU": "Cuba", "CW": "Curaçao",
  "CZ": "Czech Republic", "CI": "Côte d'Ivoire", "KP": "Dem. Rep. Korea",
  "CD": "Democratic Republic of the Congo", "DJ": "Djibouti", "DM": "Dominica",
  "DO": "Dominican Republic", "EC": "Ecuador", "EG": "Egypt", "SV": "El Salvador",
  "GQ": "Equatorial Guinea", "ER": "Eritrea", "EE": "Estonia", "ET": "Ethiopia",
  "FI": "Finland", "GF": "French Guiana", "GA": "Gabon", "GE": "Georgia",
  "DE": "Germany", "GH": "Ghana", "GL": "Greenland", "GD": "Grenada", "GU": "Guam",
  "GT": "Guatemala", "GN": "Guinea", "GW": "Guinea-Bissau", "GY": "Guyana",
  "HT": "Haiti", "HN": "Honduras", "HU": "Hungary", "IS": "Iceland",
  "IN": "India", "IR": "Iran", "IQ": "Iraq", "IE": "Ireland", "IL": "Israel",
  "JM": "Jamaica", "JO": "Jordan", "KZ": "Kazakhstan", "KE": "Kenya",
  "XK": "Kosovo", "KW": "Kuwait", "KG": "Kyrgyzstan", "LA": "Lao PDR",
  "LV": "Latvia", "LB": "Lebanon", "LS": "Lesotho", "LR": "Liberia",
  "LY": "Libya", "LT": "Lithuania", "LU": "Luxembourg", "MK": "Macedonia",
  "MG": "Madagascar", "MW": "Malawi", "MV": "Maldives", "ML": "Mali",
  "MH": "Marshall Islands", "MQ": "Martinique", "MR": "Mauritania", "YT": "Mayotte",
  "MX": "Mexico", "MD": "Moldova", "MN": "Mongolia", "ME": "Montenegro",
  "MS": "Montserrat", "MA": "Morocco", "MZ": "Mozambique", "MM": "Myanmar",
  "NA": "Namibia", "NR": "Nauru", "NP": "Nepal", "NL": "Netherlands",
  "BQBO": "Netherlands", "NI": "Nicaragua", "NE": "Niger", "NG": "Nigeria",
  "PK": "Pakistan", "PW": "Palau", "PS": "Palestine", "PA": "Panama",
  "PY": "Paraguay", "PE": "Peru", "PL": "Poland", "PT": "Portugal", "QA": "Qatar",
  "CG": "Republic of Congo", "KR": "Republic of Korea", "RE": "Reunion",
  "RO": "Romania", "RW": "Rwanda", "BQSA": "Saba (Netherlands)", "LC": "Saint Lucia",
  "VC": "Saint Vincent and the Grenadines", "BL": "Saint-Barthélemy",
  "MF": "Saint-Martin", "SA": "Saudi Arabia", "SN": "Senegal", "RS": "Serbia",
  "SL": "Sierra Leone", "SX": "Sint Maarten", "SK": "Slovakia", "SI": "Slovenia",
  "SO": "Somalia", "ZA": "South Africa", "SS": "South Sudan", "ES": "Spain",
  "LK": "Sri Lanka", "BQSE": "St. Eustatius (Netherlands)", "SD": "Sudan",
  "SR": "Suriname", "SZ": "Swaziland", "SE": "Sweden", "CH": "Switzerland",
  "SY": "Syria", "TW": "Taiwan", "TJ": "Tajikistan", "TZ": "Tanzania",
  "TH": "Thailand", "GM": "The Gambia", "TL": "Timor-Leste", "TG": "Togo",
  "TN": "Tunisia", "TM": "Turkmenistan", "TV": "Tuvalu", "UG": "Uganda",
  "UA": "Ukraine", "AE": "United Arab Emirates", "UY": "Uruguay", "UZ": "Uzbekistan",
  "VE": "Venezuela", "VN": "Vietnam", "EH": "Western Sahara", "YE": "Yemen",
  "ZM": "Zambia", "ZW": "Zimbabwe",
  // Common codes used by the embedded simplemaps SVG that were missing above.
  "AR": "Argentina", "AO": "Angola", "AU": "Australia", "CA": "Canada",
  "CN": "China", "FR": "France", "GB": "United Kingdom", "ID": "Indonesia",
  "IT": "Italy", "JP": "Japan", "NO": "Norway", "NZ": "New Zealand",
  "PH": "Philippines", "RU": "Russia", "TR": "Turkey", "US": "United States",
  "DK": "Denmark", "GR": "Greece", "MY": "Malaysia",
};

// Resolve the best display name for a clicked SVG element. The map uses
// two different conventions: single-path countries have id="XX" name="Name",
// while multi-path countries repeat class="Name" on each piece.
function resolveCountryName(el) {
  const nameAttr = el.getAttribute("name");
  if (nameAttr) return nameAttr.trim();

  const cls = (el.getAttribute("class") || "").trim();
  // A class like "Angola" looks like a country name; skip utility classes.
  if (cls && /^[A-Z][A-Za-z' .-]+$/.test(cls)) return cls;

  const id = (el.id || "").toUpperCase();
  if (countryNames[id]) return countryNames[id];
  return id || "";
}

function formatSeenDate(raw) {
  // GDELT's seendate is YYYYMMDDTHHMMSSZ. Turn it into something readable.
  if (!raw || raw.length < 15) return raw || "";
  const iso = `${raw.slice(0, 4)}-${raw.slice(4, 6)}-${raw.slice(6, 8)}T` +
              `${raw.slice(9, 11)}:${raw.slice(11, 13)}:${raw.slice(13, 15)}Z`;
  const d = new Date(iso);
  if (isNaN(d.getTime())) return raw;
  return d.toLocaleString();
}

function setStatus(msg) {
  const el = document.getElementById("newsStatus");
  if (el) el.textContent = msg;
}

function renderStories(stories) {
  const list = document.getElementById("newsList");
  if (!list) return;
  list.innerHTML = "";

  if (!stories || stories.length === 0) {
    setStatus("No recent stories found.");
    return;
  }

  setStatus(`${stories.length} recent ${stories.length === 1 ? "story" : "stories"}.`);

  for (const s of stories) {
    const li = document.createElement("li");
    li.className = "news-item";

    const a = document.createElement("a");
    a.href = s.url;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.textContent = s.title || "(untitled)";
    a.className = "news-title";

    const meta = document.createElement("div");
    meta.className = "news-meta";
    const pieces = [];
    if (s.domain) pieces.push(s.domain);
    if (s.source_country) pieces.push(s.source_country);
    if (s.seendate) pieces.push(formatSeenDate(s.seendate));
    meta.textContent = pieces.join("  \u00b7  ");

    li.appendChild(a);
    li.appendChild(meta);
    list.appendChild(li);
  }
}

let currentRequest = 0;

async function loadNewsFor(countryName) {
  if (!countryName) return;
  const myRequest = ++currentRequest;

  document.getElementById("countryName").innerText = countryName;
  setStatus(`Loading news for ${countryName}\u2026`);
  document.getElementById("newsList").innerHTML = "";

  const timespan =
    (document.getElementById("timespanSelect") || {}).value || "24h";

  try {
    const url = `/api/news?country=${encodeURIComponent(countryName)}` +
                `&timespan=${encodeURIComponent(timespan)}&max=25`;
    const resp = await fetch(url);
    // If another click raced in, ignore this (stale) response.
    if (myRequest !== currentRequest) return;

    const data = await resp.json();
    if (!resp.ok) {
      setStatus(`Error: ${data.error || resp.statusText}`);
      return;
    }
    renderStories(data.stories || []);
  } catch (err) {
    if (myRequest !== currentRequest) return;
    setStatus(`Request failed: ${err.message || err}`);
  }
}

window.onload = () => {
  const countries = document.querySelectorAll("svg path");
  countries.forEach(country => {
    country.addEventListener("click", () => {
      const name = resolveCountryName(country);
      if (!name) {
        setStatus("Could not identify that country.");
        return;
      }
      loadNewsFor(name);
    });
  });

  const ts = document.getElementById("timespanSelect");
  if (ts) {
    ts.addEventListener("change", () => {
      const current = document.getElementById("countryName").innerText;
      if (current && current !== "None" && !current.startsWith("Country:")) {
        loadNewsFor(current);
      }
    });
  }
};
