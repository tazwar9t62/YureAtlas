"use client";

import { useEffect, useRef, useState } from "react";
import * as maplibregl from "maplibre-gl";
import type { Map, GeoJSONSource } from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import type { MapData } from "@/lib/api";
import { dateLabel, percent } from "@/lib/api";

const empty: GeoJSON.FeatureCollection = {
  type: "FeatureCollection",
  features: [],
};

export default function ForecastMap({
  data,
  onSelect,
  selected,
  showCells,
  showEvents,
  redThreshold = 0.05,
  yellowThreshold = 0.01,
}: {
  data: MapData | null;
  onSelect: (id: string) => void;
  selected: string | null;
  showCells: boolean;
  showEvents: boolean;
  redThreshold?: number;
  yellowThreshold?: number;
}) {
  const container = useRef<HTMLDivElement>(null);
  const map = useRef<Map | null>(null);
  const callback = useRef(onSelect);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    callback.current = onSelect;
  }, [onSelect]);
  useEffect(() => {
    if (!container.current) return;
    let instance: Map;
    try {
      instance = new maplibregl.Map({
        container: container.current,
        center: [139, 35.3],
        zoom: 4.4,
        minZoom: 2.5,
        maxZoom: 9,
        attributionControl: false,
        style: {
          version: 8,
          sources: {
            land: {
              type: "geojson",
              data: "/land.geojson",
              attribution: "Made with Natural Earth · public domain",
            },
          },
          layers: [
            {
              id: "ocean",
              type: "background",
              paint: { "background-color": "#0b1723" },
            },
            {
              id: "land",
              type: "fill",
              source: "land",
              paint: { "fill-color": "#263849" },
            },
            {
              id: "coast",
              type: "line",
              source: "land",
              paint: { "line-color": "#7492a6", "line-width": 0.75 },
            },
          ],
        },
      });
      map.current = instance;
      for (const [label, lon, lat] of [
        ["HOKKAIDO", 142.7, 43.6],
        ["HONSHU", 138.1, 36.5],
        ["KYUSHU", 130.8, 32.6],
        ["NORTH PACIFIC OCEAN", 148, 31],
      ] as const) {
        const element = document.createElement("div");
        element.className = "geo-label";
        element.textContent = label;
        new maplibregl.Marker({ element })
          .setLngLat([lon, lat])
          .addTo(instance);
      }
      instance.addControl(
        new maplibregl.NavigationControl({ showCompass: false }),
        "bottom-right",
      );
      instance.addControl(
        new maplibregl.AttributionControl({ compact: true }),
        "bottom-left",
      );
      instance.on("load", () => {
        const lines: GeoJSON.Feature[] = [];
        for (let lon = 120; lon <= 160; lon += 5)
          lines.push({
            type: "Feature",
            properties: {},
            geometry: {
              type: "LineString",
              coordinates: [
                [lon, 15],
                [lon, 55],
              ],
            },
          });
        for (let lat = 20; lat <= 50; lat += 5)
          lines.push({
            type: "Feature",
            properties: {},
            geometry: {
              type: "LineString",
              coordinates: [
                [115, lat],
                [165, lat],
              ],
            },
          });
        instance.addSource("graticule", {
          type: "geojson",
          data: { type: "FeatureCollection", features: lines },
        });
        instance.addLayer({
          id: "graticule",
          type: "line",
          source: "graticule",
          paint: {
            "line-color": "#668297",
            "line-opacity": 0.16,
            "line-width": 1,
            "line-dasharray": [2, 4],
          },
        });
        instance.addSource("forecast", {
          type: "geojson",
          data: empty,
          promoteId: "cell_id",
        });
        instance.addLayer({
          id: "forecast-fill",
          type: "fill",
          source: "forecast",
          paint: {
            "fill-color": [
              "match",
              ["get", "band"],
              "red",
              "#f1776d",
              "yellow",
              "#dec66a",
              "green",
              "#57b899",
              "#748494",
            ],
            "fill-opacity": [
              "match",
              ["get", "band"],
              "red",
              0.76,
              "yellow",
              0.53,
              "green",
              0.14,
              0.18,
            ],
          },
        });
        instance.addLayer({
          id: "forecast-grid",
          type: "line",
          source: "forecast",
          paint: {
            "line-color": "#8aaabc",
            "line-opacity": 0.13,
            "line-width": 0.5,
          },
        });
        instance.addLayer({
          id: "selected",
          type: "line",
          source: "forecast",
          filter: ["==", ["get", "cell_id"], ""],
          paint: { "line-color": "#b4fbff", "line-width": 2.5 },
        });
        instance.addSource("events", { type: "geojson", data: empty });
        instance.addLayer({
          id: "events",
          type: "circle",
          source: "events",
          paint: {
            "circle-radius": [
              "interpolate",
              ["linear"],
              ["get", "magnitude"],
              4,
              4,
              7,
              9,
            ],
            "circle-color": "#edf8fa",
            "circle-opacity": 0.9,
            "circle-stroke-color": "#0a1828",
            "circle-stroke-width": 1.7,
          },
        });
        instance.on("click", "forecast-fill", (e) => {
          const cell = e.features?.[0]?.properties?.cell_id;
          if (cell) callback.current(String(cell));
        });
        instance.on("mouseenter", "forecast-fill", () => {
          instance.getCanvas().style.cursor = "crosshair";
        });
        instance.on("mouseleave", "forecast-fill", () => {
          instance.getCanvas().style.cursor = "";
        });
        instance.on("click", "events", (e) => {
          const f = e.features?.[0];
          if (!f || f.geometry.type !== "Point") return;
          const p = f.properties;
          const text = document.createElement("div");
          text.className = "event-popup";
          text.textContent = `M ${Number(p.magnitude).toFixed(1)} ${p.magnitude_type} · ${p.depth_km == null ? "Depth unknown" : `${Number(p.depth_km).toFixed(1)} km`} · ${dateLabel(String(p.time), true)} JST`;
          new maplibregl.Popup()
            .setLngLat(f.geometry.coordinates as [number, number])
            .setDOMContent(text)
            .addTo(instance);
        });
        setReady(true);
      });
      instance.on("error", (e) => setError(e.error.message));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Map could not start");
    }
    return () => {
      instance?.remove();
      map.current = null;
      setReady(false);
    };
  }, []);
  useEffect(() => {
    if (!ready || !map.current) return;
    (map.current.getSource("forecast") as GeoJSONSource).setData(
      data?.cells ?? empty,
    );
    (map.current.getSource("events") as GeoJSONSource).setData(
      data?.events ?? empty,
    );
  }, [data, ready]);
  useEffect(() => {
    if (!ready || !map.current) return;
    for (const layer of ["forecast-fill", "forecast-grid"])
      map.current.setLayoutProperty(
        layer,
        "visibility",
        showCells ? "visible" : "none",
      );
    map.current.setLayoutProperty(
      "events",
      "visibility",
      showEvents ? "visible" : "none",
    );
    map.current.setFilter("selected", [
      "==",
      ["get", "cell_id"],
      selected ?? "",
    ]);
  }, [showCells, showEvents, selected, ready]);
  return (
    <div className="map-wrap">
      <div
        ref={container}
        className="map-canvas"
        role="region"
        aria-label="Japan region probability map"
        data-testid="forecast-map"
      />
      <div className="map-coordinate">
        JAPAN REGION <span>20–48° N / 122–156° E</span>
      </div>
      {error && (
        <div className="map-error" role="status">
          Map unavailable: {error}. Cell probabilities remain available in the
          selector.
        </div>
      )}
      <div className="map-legend">
        <span className="legend-title">24H OCCURRENCE PROBABILITY</span>
        <span>
          <i className="dot green" /> &lt; {percent(yellowThreshold, 0)}
        </span>
        <span>
          <i className="dot yellow" /> {percent(yellowThreshold, 0)}–&lt;
          {percent(redThreshold, 0)}
        </span>
        <span>
          <i className="dot red" /> ≥ {percent(redThreshold, 0)}
        </span>
        <span>
          <i className="dot grey" /> Unavailable
        </span>
      </div>
    </div>
  );
}
