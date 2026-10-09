build:
	docker build -t sensor-network-pwa .

run:
	docker run -p 8080:8080 -v $(CURDIR)/config.json:/app/config.json:ro -v $(CURDIR)/data:/data -d sensor-network-pwa
