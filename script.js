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

// Resolve the best display name and ISO code for a clicked SVG element.
// The current map encodes the name in a `title` attribute and the ISO
// alpha-2 code in `id`; older simplemaps exports used `name` or repeated
// `class="Country"` for multi-path countries. Returns {name, code}.
function resolveCountry(el) {
  const titleAttr = el.getAttribute("title");
  const nameAttr = el.getAttribute("name");
  const cls = (el.getAttribute("class") || "").trim();
  const rawId = (el.id || "").toUpperCase();
  // Some IDs include subregion suffixes like "UM-DQ"; the country code is
  // the part before the dash.
  const code = rawId.split("-")[0];

  let name = "";
  if (titleAttr) name = titleAttr.trim();
  else if (nameAttr) name = nameAttr.trim();
  else if (cls && cls !== "land" && /^[A-Z][A-Za-z' .-]+$/.test(cls)) name = cls;
  else if (countryNames[code]) name = countryNames[code];
  else name = code || "";

  return { name, code };
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

  const noun = stories.length === 1 ? "story" : "stories";
  setStatus(`${stories.length} recent ${noun} via ${getSource()}.`);

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
let currentAbort = null;
// Most-recently selected country, so toggling source/timespan can refetch
// without requiring another click.
let activeCountry = null;

// Hard ceiling for a single news request. Slightly above the server's
// retry budget (15s timeout x 2 retries + backoffs) so slow-but-successful
// calls still make it through, but runaway hangs don't pin the UI.
const CLIENT_TIMEOUT_MS = 45000;

function getSource() {
  const el = document.getElementById("sourceSelect");
  return (el && el.value) || "gdelt";
}

function getTimespan() {
  const el = document.getElementById("timespanSelect");
  return (el && el.value) || "24h";
}

async function loadNewsFor(country) {
  if (!country || !country.name) return;
  activeCountry = country;
  const myRequest = ++currentRequest;

  // Abort any previous in-flight request so rapid clicks don't pile up.
  if (currentAbort) currentAbort.abort();
  const controller = new AbortController();
  currentAbort = controller;
  const timer = setTimeout(() => controller.abort(), CLIENT_TIMEOUT_MS);

  const source = getSource();
  const timespan = getTimespan();

  document.getElementById("countryName").innerText = country.name;
  setStatus(`Loading news for ${country.name} via ${source}\u2026`);
  document.getElementById("newsList").innerHTML = "";

  try {
    const params = new URLSearchParams({
      country: country.name,
      country_code: country.code || "",
      source,
      timespan,
      max: "25",
    });
    const resp = await fetch(`/api/news?${params}`, { signal: controller.signal });
    if (myRequest !== currentRequest) return;

    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      setStatus(`Error (${source}): ${data.error || resp.statusText}`);
      return;
    }
    renderStories(data.stories || []);
  } catch (err) {
    if (myRequest !== currentRequest) return;
    if (err && err.name === "AbortError") {
      setStatus(`Timed out after ${CLIENT_TIMEOUT_MS / 1000}s. ` +
                `Upstream (${source}) may be rate-limiting \u2014 try again.`);
    } else {
      setStatus(`Request failed: ${err.message || err}`);
    }
  } finally {
    clearTimeout(timer);
    if (currentAbort === controller) currentAbort = null;
  }
}

window.onload = () => {
  const countries = document.querySelectorAll("svg path");
  countries.forEach(country => {
    country.addEventListener("click", () => {
      const info = resolveCountry(country);
      if (!info.name) {
        setStatus("Could not identify that country.");
        return;
      }
      loadNewsFor(info);
    });
  });

  // Re-fetch when the user changes source or timespan, as long as we have
  // a country selected.
  for (const id of ["sourceSelect", "timespanSelect"]) {
    const el = document.getElementById(id);
    if (el) {
      el.addEventListener("change", () => {
        if (activeCountry) loadNewsFor(activeCountry);
      });
    }
  }
};
