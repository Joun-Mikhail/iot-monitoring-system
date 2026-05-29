-- IoT Rule: forward all sensor messages to Lambda
-- Source topic: iot/sensors/data
-- Action: Lambda invoke (process_sensor_data)
SELECT * FROM 'iot/sensors/data'
