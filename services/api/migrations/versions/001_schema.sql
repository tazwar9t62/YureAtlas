CREATE EXTENSION IF NOT EXISTS postgis;


CREATE TABLE catalogue_sources (
	name VARCHAR NOT NULL, 
	provider VARCHAR NOT NULL, 
	attribution TEXT NOT NULL, 
	status VARCHAR NOT NULL, 
	details JSONB NOT NULL, 
	id VARCHAR(36) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (name)
)

;

CREATE INDEX ix_catalogue_sources_created_at ON catalogue_sources (created_at);


CREATE TABLE grid_versions (
	geometry_hash VARCHAR(64) NOT NULL, 
	spec JSONB NOT NULL, 
	id VARCHAR(36) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (geometry_hash)
)

;

CREATE INDEX ix_grid_versions_created_at ON grid_versions (created_at);


CREATE TABLE jobs (
	logical_key VARCHAR NOT NULL, 
	kind VARCHAR NOT NULL, 
	payload JSONB NOT NULL, 
	status VARCHAR NOT NULL, 
	attempts INTEGER NOT NULL, 
	progress FLOAT NOT NULL, 
	heartbeat TIMESTAMP WITH TIME ZONE, 
	lease_until TIMESTAMP WITH TIME ZONE, 
	cancel_requested BOOLEAN NOT NULL, 
	error TEXT, 
	result JSONB NOT NULL, 
	id VARCHAR(36) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (logical_key)
)

;

CREATE INDEX ix_jobs_created_at ON jobs (created_at);

CREATE INDEX ix_jobs_status ON jobs (status);


CREATE TABLE worker_state (
	id VARCHAR NOT NULL, 
	heartbeat TIMESTAMP WITH TIME ZONE NOT NULL, 
	details JSONB NOT NULL, 
	PRIMARY KEY (id)
)

;


CREATE TABLE catalogue_snapshots (
	source_id VARCHAR(36) NOT NULL, 
	digest VARCHAR(64) NOT NULL, 
	cutoff TIMESTAMP WITH TIME ZONE NOT NULL, 
	policy VARCHAR NOT NULL, 
	revision_ids JSONB NOT NULL, 
	id VARCHAR(36) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(source_id) REFERENCES catalogue_sources (id), 
	UNIQUE (digest)
)

;

CREATE INDEX ix_catalogue_snapshots_created_at ON catalogue_snapshots (created_at);

CREATE INDEX ix_catalogue_snapshots_source_id ON catalogue_snapshots (source_id);


CREATE TABLE earthquake_events (
	source_id VARCHAR(36) NOT NULL, 
	provider_id VARCHAR NOT NULL, 
	id VARCHAR(36) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (source_id, provider_id), 
	FOREIGN KEY(source_id) REFERENCES catalogue_sources (id)
)

;

CREATE INDEX ix_earthquake_events_created_at ON earthquake_events (created_at);

CREATE INDEX ix_earthquake_events_source_id ON earthquake_events (source_id);


CREATE TABLE experiment_versions (
	name VARCHAR NOT NULL, 
	source_id VARCHAR(36) NOT NULL, 
	grid_id VARCHAR(36) NOT NULL, 
	config_hash VARCHAR(64) NOT NULL, 
	config JSONB NOT NULL, 
	synthetic BOOLEAN NOT NULL, 
	id VARCHAR(36) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (name), 
	FOREIGN KEY(source_id) REFERENCES catalogue_sources (id), 
	FOREIGN KEY(grid_id) REFERENCES grid_versions (id)
)

;

CREATE INDEX ix_experiment_versions_created_at ON experiment_versions (created_at);

CREATE INDEX ix_experiment_versions_grid_id ON experiment_versions (grid_id);

CREATE INDEX ix_experiment_versions_source_id ON experiment_versions (source_id);


CREATE TABLE grid_cells (
	grid_id VARCHAR(36) NOT NULL, 
	id VARCHAR NOT NULL, 
	ix INTEGER NOT NULL, 
	iy INTEGER NOT NULL, 
	area_km2 FLOAT NOT NULL, 
	geometry geometry(POLYGON,4326) NOT NULL, 
	geojson JSONB NOT NULL, 
	PRIMARY KEY (grid_id, id), 
	FOREIGN KEY(grid_id) REFERENCES grid_versions (id)
)

;

CREATE INDEX idx_grid_cells_geometry ON grid_cells USING gist (geometry);


CREATE TABLE ingestion_runs (
	source_id VARCHAR(36) NOT NULL, 
	start TIMESTAMP WITH TIME ZONE NOT NULL, 
	"end" TIMESTAMP WITH TIME ZONE NOT NULL, 
	status VARCHAR NOT NULL, 
	report JSONB NOT NULL, 
	id VARCHAR(36) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(source_id) REFERENCES catalogue_sources (id)
)

;

CREATE INDEX ix_ingestion_runs_created_at ON ingestion_runs (created_at);

CREATE INDEX ix_ingestion_runs_end ON ingestion_runs ("end");

CREATE INDEX ix_ingestion_runs_source_id ON ingestion_runs (source_id);

CREATE INDEX ix_ingestion_runs_start ON ingestion_runs (start);


CREATE TABLE earthquake_revisions (
	event_id VARCHAR(36) NOT NULL, 
	origin_time TIMESTAMP WITH TIME ZONE NOT NULL, 
	provider_updated TIMESTAMP WITH TIME ZONE, 
	received_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	longitude FLOAT NOT NULL, 
	latitude FLOAT NOT NULL, 
	depth_km FLOAT, 
	magnitude FLOAT NOT NULL, 
	magnitude_type VARCHAR NOT NULL, 
	status VARCHAR NOT NULL, 
	content_hash VARCHAR(64) NOT NULL, 
	raw_ref VARCHAR NOT NULL, 
	location geometry(POINT,4326) NOT NULL, 
	id VARCHAR(36) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (event_id, content_hash), 
	FOREIGN KEY(event_id) REFERENCES earthquake_events (id)
)

;

CREATE INDEX idx_earthquake_revisions_location ON earthquake_revisions USING gist (location);

CREATE INDEX ix_earthquake_revisions_created_at ON earthquake_revisions (created_at);

CREATE INDEX ix_earthquake_revisions_event_id ON earthquake_revisions (event_id);

CREATE INDEX ix_earthquake_revisions_origin_time ON earthquake_revisions (origin_time);

CREATE INDEX ix_earthquake_revisions_received_at ON earthquake_revisions (received_at);


CREATE TABLE feature_sets (
	experiment_id VARCHAR(36) NOT NULL, 
	schema_version VARCHAR NOT NULL, 
	artifact VARCHAR NOT NULL, 
	partitions JSONB NOT NULL, 
	id VARCHAR(36) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(experiment_id) REFERENCES experiment_versions (id)
)

;

CREATE INDEX ix_feature_sets_created_at ON feature_sets (created_at);

CREATE INDEX ix_feature_sets_experiment_id ON feature_sets (experiment_id);


CREATE TABLE model_versions (
	experiment_id VARCHAR(36) NOT NULL, 
	algorithm VARCHAR NOT NULL, 
	status VARCHAR NOT NULL, 
	training_cutoff TIMESTAMP WITH TIME ZONE NOT NULL, 
	available_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	snapshot_id VARCHAR(36), 
	artifact VARCHAR, 
	metadata JSONB NOT NULL, 
	id VARCHAR(36) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(experiment_id) REFERENCES experiment_versions (id), 
	FOREIGN KEY(snapshot_id) REFERENCES catalogue_snapshots (id)
)

;

CREATE INDEX ix_model_versions_created_at ON model_versions (created_at);

CREATE INDEX ix_model_versions_experiment_id ON model_versions (experiment_id);


CREATE TABLE forecast_runs (
	logical_key VARCHAR NOT NULL, 
	experiment_id VARCHAR(36) NOT NULL, 
	model_id VARCHAR(36) NOT NULL, 
	snapshot_id VARCHAR(36) NOT NULL, 
	issued_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	start TIMESTAMP WITH TIME ZONE NOT NULL, 
	"end" TIMESTAMP WITH TIME ZONE NOT NULL, 
	input_cutoff TIMESTAMP WITH TIME ZONE NOT NULL, 
	mode VARCHAR NOT NULL, 
	status VARCHAR NOT NULL, 
	provenance JSONB NOT NULL, 
	id VARCHAR(36) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (logical_key), 
	CHECK ("end" > start), 
	FOREIGN KEY(experiment_id) REFERENCES experiment_versions (id), 
	FOREIGN KEY(model_id) REFERENCES model_versions (id), 
	FOREIGN KEY(snapshot_id) REFERENCES catalogue_snapshots (id)
)

;

CREATE INDEX ix_forecast_runs_created_at ON forecast_runs (created_at);

CREATE INDEX ix_forecast_runs_end ON forecast_runs ("end");

CREATE INDEX ix_forecast_runs_experiment_id ON forecast_runs (experiment_id);

CREATE INDEX ix_forecast_runs_mode ON forecast_runs (mode);

CREATE INDEX ix_forecast_runs_model_id ON forecast_runs (model_id);

CREATE INDEX ix_forecast_runs_start ON forecast_runs (start);


CREATE TABLE model_promotions (
	experiment_id VARCHAR(36) NOT NULL, 
	model_id VARCHAR(36) NOT NULL, 
	reason TEXT NOT NULL, 
	id VARCHAR(36) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(experiment_id) REFERENCES experiment_versions (id), 
	FOREIGN KEY(model_id) REFERENCES model_versions (id)
)

;

CREATE INDEX ix_model_promotions_created_at ON model_promotions (created_at);

CREATE INDEX ix_model_promotions_experiment_id ON model_promotions (experiment_id);


CREATE TABLE evaluation_runs (
	forecast_id VARCHAR(36) NOT NULL, 
	snapshot_id VARCHAR(36) NOT NULL, 
	version INTEGER NOT NULL, 
	status VARCHAR NOT NULL, 
	reason TEXT NOT NULL, 
	metrics JSONB NOT NULL, 
	id VARCHAR(36) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (forecast_id, snapshot_id), 
	FOREIGN KEY(forecast_id) REFERENCES forecast_runs (id), 
	FOREIGN KEY(snapshot_id) REFERENCES catalogue_snapshots (id)
)

;

CREATE INDEX ix_evaluation_runs_created_at ON evaluation_runs (created_at);

CREATE INDEX ix_evaluation_runs_forecast_id ON evaluation_runs (forecast_id);


CREATE TABLE forecast_cells (
	forecast_id VARCHAR(36) NOT NULL, 
	cell_id VARCHAR NOT NULL, 
	probability FLOAT NOT NULL, 
	expected_count FLOAT, 
	band VARCHAR NOT NULL, 
	PRIMARY KEY (forecast_id, cell_id), 
	CHECK (probability >= 0 AND probability <= 1 AND probability != 'NaN'::float8), 
	FOREIGN KEY(forecast_id) REFERENCES forecast_runs (id)
)

;


CREATE TABLE evaluation_cells (
	evaluation_id VARCHAR(36) NOT NULL, 
	cell_id VARCHAR NOT NULL, 
	observed_count INTEGER NOT NULL, 
	outcome VARCHAR NOT NULL, 
	PRIMARY KEY (evaluation_id, cell_id), 
	FOREIGN KEY(evaluation_id) REFERENCES evaluation_runs (id)
)

;