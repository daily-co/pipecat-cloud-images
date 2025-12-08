# Local Testing Environment

A mock server that emulates **Pipecat Cloud's** core functionality for local container testing. 

This tool allows developers to test their Pipecat containers locally by simulating the cloud environment's essential behaviors and routes.

---

## Prerequisites

Before getting started, ensure you have:

* **Docker** installed and running
* **Python 3.12** or higher

---

## Setup Instructions

### 1. Build and Run Your Container

Build and run your local container:

```bash
# Build the container
# Replace 'smallwebrtc' with your application name
docker build -t pipecat-smallwebrtc:latest .

# Run the container
docker run -it -p 8080:8080 pipecat-smallwebrtc
```

---

### 2. Configure Environment Variables

1. Copy the example environment file:

```bash
cp env.example .env
```

2. Update `.env` with your local configuration:

* `LOCAL_POD_IP`: Usually `localhost`
* `LOCAL_POD_PORT`: The port your container is running on (e.g., `8080`)

---

### 3. Start the Mock Server

Run the server with:

```bash
python mock_pipecat_cloud.py
```

The server will now simulate the Pipecat cloud environment locally.

---

## Available Routes

The mock server currently supports these endpoints:

1. **Start Bot**

   * **Path:** `/v1/public/{agent_name}/start`
   * **Purpose:** Initiates a new bot session
   * **Docs:** [Active Sessions](https://docs.pipecat.ai/deployment/pipecat-cloud/fundamentals/active-sessions)

2. **Custom Endpoint**

   * **Path:** `/v1/public/{agent_name}/sessions/{session_id}/{path:path}`
   * **Purpose:** Handles custom endpoint invocations for running pods

---

## Testing

After setup, you can test your container's functionality using the mock server. 
All requests and responses try to mimic the cloud environment.

---

## Limitations

* Simplified version of the Pipecat cloud
* Only core routes are implemented
* Intended for basic testing and development only
