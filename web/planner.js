"use strict";

const byId = (id) => document.getElementById(id);
const form = byId("trip-form");
const origin = byId("origin");
const destination = byId("destination");
const progress = byId("progress");
const resultPanel = byId("result");
const mapNotice = byId("map-notice");
const fitButton = byId("fit-route");
const stationsButton = byId("all-stations");
const stationStatus = byId("station-status");
const busyButtons = ["submit", "example-stop", "example-direct"].map(byId);
const locations = {
    Vienna: { lon: 16.3692842, lat: 48.2024484 },
    Graz: { lon: 15.4392, lat: 47.0667 },
    Linz: { lon: 14.2858, lat: 48.2914 },
};

const number = new Intl.NumberFormat("en", { maximumFractionDigits: 2 });
const litres = new Intl.NumberFormat("en", { maximumFractionDigits: 3 });
const euros = new Intl.NumberFormat("en", { style: "currency", currency: "EUR" });
const unitPrice = new Intl.NumberFormat("en", { style: "currency", currency: "EUR", maximumFractionDigits: 3 });
const distance = (km) => Number(km) > 0 && Number(km) < 1
    ? `${number.format(Number(km) * 1000)} m` : `${number.format(km)} km`;
const fulfilled = new Set(["direct", "stop", "prices_unavailable", "no_feasible_stop"]);
const errorMessages = {
    outside_austria: "Choose an origin and destination in Austria.",
    no_route: "No drivable route was found between those points.",
    border_restricted_route: "The routing service chose a road outside Austria. Try another trip.",
    routing_unavailable: "Road routing is unavailable. Check that OSRM is running.",
    boundary_unavailable: "The worker could not read the Austrian map boundary.",
    stations_unavailable: "The station catalogue is unavailable. Check the worker’s local data.",
    station_capacity_unavailable: "The online station lookup returned too many matches. Use the local station catalogue.",
    station_catalogue_unavailable: "The local station catalogue is unavailable. Try again once the worker is ready.",
    worker_unavailable: "The planning worker is unavailable. Try again once it’s running.",
    worker_timeout: "The planning worker timed out. Try again.",
    attempt_expired: "The planning job expired. Try again.",
    job_deadline_exceeded: "The trip could not finish within 90 seconds, including time in the queue.",
    database_unavailable: "The trip database is unavailable. Try again shortly.",
};

for (const select of [origin, destination]) {
    for (const city of Object.keys(locations)) select.add(new Option(city, city));
}
destination.value = "Graz";

let map;
let mapLayers;
let routeBounds;
let stationLayer;
let stationRenderer;
if (window.L) {
    map = L.map("map", { scrollWheelZoom: false }).setView([47.65, 14.9], 7);
    mapLayers = L.featureGroup().addTo(map);
    map.createPane("stationCatalogue").style.zIndex = "350";
    stationRenderer = L.canvas({ pane: "stationCatalogue" });
    stationsButton.disabled = false;
    map.attributionControl.setPrefix(false);
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19,
        attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    }).on("tileerror", () => {
        mapNotice.textContent = "Some background map tiles could not load.";
        mapNotice.hidden = false;
    }).on("loading", () => {
        mapNotice.hidden = true;
    }).addTo(map);
    map.attributionControl.addAttribution("Road routing by OSRM");
} else {
    mapNotice.textContent = "The map library couldn’t load. Trip results are still available below.";
    mapNotice.hidden = false;
}

function setProgress(message, state) {
    progress.textContent = message;
    progress.dataset.state = state;
}

function fitRoute() {
    if (routeBounds?.isValid()) {
        map.fitBounds(routeBounds, { padding: [47, 47], maxZoom: 13, animate: false });
    }
}

function stationPopup(station) {
    const popup = document.createElement("div");
    const name = document.createElement("strong");
    const fuel = document.createElement("p");
    name.textContent = station.name;
    const labels = { diesel: "Diesel", petrol95: "Petrol 95" };
    fuel.textContent = `${station.fuel_types.map((type) => labels[type] || type).join(" · ")} | Open ${station.opening_hours}`;
    popup.append(name, fuel);
    return popup;
}

async function toggleStations() {
    const shown = stationsButton.getAttribute("aria-pressed") === "true";
    if (shown) {
        map.closePopup();
        stationLayer.remove();
        stationsButton.setAttribute("aria-pressed", "false");
        byId("catalogue-legend").hidden = true;
        stationStatus.textContent = "";
        fitRoute();
        return;
    }

    stationsButton.disabled = true;
    stationStatus.textContent = "Loading stations…";
    try {
        if (!stationLayer) {
            const { stations } = await request("/stations");
            if (!stations.length) {
                stationStatus.textContent = "The catalogue has no stations.";
                return;
            }
            const dots = stations.map((station) => L.circleMarker([station.point.lat, station.point.lon], {
                renderer: stationRenderer, radius: 5, color: "#f0fff9", weight: 1.4,
                fillColor: "#168066", fillOpacity: .88,
            }).bindPopup(() => stationPopup(station)));
            stationLayer = L.featureGroup(dots);
        }
        stationLayer.addTo(map);
        stationsButton.setAttribute("aria-pressed", "true");
        byId("catalogue-legend").hidden = false;
        stationStatus.textContent = `${number.format(stationLayer.getLayers().length)} stations in the local catalogue. Click a dot for details.`;
        map.fitBounds(stationLayer.getBounds(), { padding: [32, 32], maxZoom: 13, animate: false });
    } catch (error) {
        stationStatus.textContent = error.name === "TypeError" || error.name === "TimeoutError"
            ? "Couldn’t load the station catalogue. Check that Docker is running."
            : error.message;
    } finally {
        stationsButton.disabled = false;
    }
}

function marker(point, text, symbol, kind) {
    const icon = L.divIcon({ className: `map-marker ${kind}`, html: symbol, iconSize: [27, 27] });
    const label = document.createElement("span");
    label.textContent = text;
    L.marker([point.lat, point.lon], { icon, title: text, alt: text })
        .bindTooltip(label, { direction: "top", offset: [0, -13] })
        .addTo(mapLayers);
}

function drawTrip(start, end, result) {
    byId("alternate-legend").hidden = !result?.runner_up || !map;
    if (!map) return;
    mapLayers.clearLayers();
    const alternative = result?.runner_up;
    if (alternative) {
        L.geoJSON(alternative.route.geometry, {
            style: { color: "#7748b7", weight: 5, dashArray: "8 9", opacity: .9 },
        }).addTo(mapLayers);
        marker(alternative.station.point, `Runner-up · ${alternative.station.name}`, "2", "alternate");
    }
    if (result?.route) {
        L.geoJSON(result.route.geometry, { style: { color: "#fff", weight: 9, opacity: .9 } }).addTo(mapLayers);
        L.geoJSON(result.route.geometry, { style: { color: "#016bce", weight: 5 } }).addTo(mapLayers);
    }
    marker(locations[start], `Origin · ${start}`, "A", "start");
    marker(locations[end], `Destination · ${end}`, "B", "end");
    if (result?.station) marker(result.station.point, `Fuel stop · ${result.station.name}`, "F", "fuel");
    routeBounds = mapLayers.getBounds();
    fitButton.disabled = !routeBounds.isValid();
    if (stationsButton.getAttribute("aria-pressed") !== "true") fitRoute();
}

function addMetric(label, value) {
    const item = document.createElement("div");
    const title = document.createElement("dt");
    const amount = document.createElement("dd");
    title.textContent = label;
    amount.textContent = value;
    item.append(title, amount);
    byId("metrics").append(item);
}

function showNote(id, message) {
    const note = byId(id);
    note.textContent = message;
    note.hidden = !message;
}

function showResult(start, end, trip, result) {
    const outcomes = {
        direct: ["Direct route", "No fuel stop needed"],
        stop: ["Lowest estimated cost", result.station?.name],
        prices_unavailable: ["Least-detour stop", result.station?.name],
        no_feasible_stop: ["No fuel stop found", "No feasible single stop"],
    };
    const [outcome, title] = outcomes[result.status];
    byId("outcome").textContent = outcome;
    byId("result-title").textContent = title;
    resultPanel.dataset.outcome = result.status;
    byId("metrics").replaceChildren();

    if (result.route) {
        addMetric("Distance", distance(result.route.distance_km));
        addMetric("Driving time", `${Math.round(Number(result.route.duration_s) / 60)} min`);
    }
    if (result.station) {
        addMetric("Buy", `${litres.format(result.litres_to_buy)} L`);
        addMetric("Detour", distance(result.detour_km));
        addMetric("Est. cost", result.estimated_cost_eur == null ? "Unavailable" : euros.format(result.estimated_cost_eur));
    }
    if (result.price_eur_per_l != null) addMetric("Price per litre", unitPrice.format(result.price_eur_per_l));
    if (result.arrival_fuel_l != null) addMetric("Arrival fuel", `${litres.format(result.arrival_fuel_l)} L`);

    let priceNote = "";
    if (result.status === "prices_unavailable") priceNote = "No usable fuel prices. This stop adds the least driving distance.";
    if (result.status === "stop" && !result.ranking_complete) priceNote = "Lowest cost among feasible stations with usable prices.";
    if (result.status === "no_feasible_stop") priceNote = "The stations checked did not meet the trip’s fuel and detour requirements.";
    showNote("price-note", priceNote);
    const next = result.runner_up;
    showNote("runner-up", next
        ? `Runner-up: ${next.station.name} · ${euros.format(next.estimated_cost_eur)} · ${euros.format(result.savings_eur)} savings at the recommended stop`
        : "");
    showNote("coverage", result.status === "direct" ? "" :
        `${result.stations_compared} checked · ${result.feasible_stations} feasible · ${result.priced_stations} priced`);

    const maps = byId("open-maps");
    maps.hidden = !result.route;
    if (result.route) {
        const coordinates = ({ lat, lon }) => `${lat},${lon}`;
        const url = new URL("https://www.google.com/maps/dir/");
        url.search = new URLSearchParams({
            api: "1",
            origin: coordinates(trip.origin),
            destination: coordinates(trip.destination),
            travelmode: "driving",
        });
        if (result.station) url.searchParams.set("waypoints", coordinates(result.station.point));
        maps.href = url.href;
    }
    resultPanel.hidden = false;
    drawTrip(start, end, result);
}

async function request(path, options) {
    const response = await fetch(path, { ...options, signal: AbortSignal.timeout(8000) });
    const payload = await response.json();
    if (!response.ok) {
        if (response.status === 422) throw new Error("Check the trip and fuel values, then try again.");
        throw new Error(errorMessages[payload.detail?.status] || `The trip API returned HTTP ${response.status}.`);
    }
    return payload;
}

async function planTrip(event) {
    event.preventDefault();
    const [start, end] = [origin.value, destination.value];
    const values = new FormData(form);
    if (start === end) {
        setProgress("Choose a different destination.", "error");
        return;
    }
    const trip = {
        origin: locations[start],
        destination: locations[end],
        fuel_type: values.get("fuel_type"),
        current_fuel_l: Number(values.get("current_fuel_l")),
        tank_capacity_l: Number(values.get("tank_capacity_l")),
        consumption_l_per_100km: Number(values.get("consumption_l_per_100km")),
        arrival_reserve_l: Number(values.get("arrival_reserve_l")),
        max_detour_km: values.get("max_detour_km") === "" ? null : Number(values.get("max_detour_km")),
    };
    if (Math.max(trip.current_fuel_l, trip.arrival_reserve_l, 1) > trip.tank_capacity_l) {
        setProgress("Current fuel, reserve, and the 1 L stop buffer must fit in the tank.", "error");
        return;
    }

    form.setAttribute("aria-busy", "true");
    busyButtons.forEach((button) => { button.disabled = true; });
    resultPanel.hidden = true;
    byId("route-caption").textContent = `${start} to ${end}`;
    drawTrip(start, end);
    setProgress("Submitting trip…", "pending");
    try {
        const receipt = await request("/trip-plans", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(trip),
        });
        const deadline = performance.now() + 95000;
        const explainWait = {
            QUEUED: "Queued for the planning worker…",
            RUNNING: "Checking roads and fuel stops…",
        };
        let job = receipt;
        while (explainWait[job.status] && performance.now() < deadline) {
            if (progress.textContent !== explainWait[job.status]) setProgress(explainWait[job.status], "pending");
            await new Promise((resolve) => setTimeout(resolve, 600));
            job = await request(`/trip-plans/${encodeURIComponent(receipt.id)}`);
        }
        if (job.status === "FAILED") throw new Error(errorMessages[job.error?.status] || "The worker couldn’t plan this trip.");
        if (job.status !== "SUCCEEDED") throw new Error("The trip is still processing. Try again shortly.");
        if (!fulfilled.has(job.result?.status)) throw new Error("The worker returned an unrecognized result.");
        showResult(start, end, trip, job.result);
        setProgress("Trip checked", "complete");
    } catch (error) {
        setProgress(error.name === "TypeError" || error.name === "TimeoutError"
            ? "Couldn’t reach the local trip API. Check that Docker is running."
            : error.message, "error");
    } finally {
        form.removeAttribute("aria-busy");
        busyButtons.forEach((button) => { button.disabled = false; });
    }
}

function runExample(fuel) {
    form.reset();
    origin.value = "Vienna";
    destination.value = "Graz";
    form.elements.current_fuel_l.value = fuel;
    form.requestSubmit();
}

form.addEventListener("submit", planTrip);
byId("swap").addEventListener("click", () => {
    [origin.value, destination.value] = [destination.value, origin.value];
});
byId("example-stop").addEventListener("click", () => runExample(8));
byId("example-direct").addEventListener("click", () => runExample(35));
fitButton.addEventListener("click", fitRoute);
stationsButton.addEventListener("click", toggleStations);
runExample(8);
