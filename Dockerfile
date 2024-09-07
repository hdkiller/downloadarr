# Use the official Python image from the Docker Hub
FROM python:3.9-slim

# Set the working directory in the container
WORKDIR /app

# Copy the current directory contents into the container at /app
COPY run-downloadarr.sh downloadarr.py requirements.txt /app/

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Make run-downloadarr.sh executable
RUN chmod +x run-downloadarr.sh

# Run run-downloadarr.sh when the container launches
CMD ["./run-downloadarr.sh"]
