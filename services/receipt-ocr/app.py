import boto3
import base64
import google.generativeai as genai
import os
import psycopg2
import json
import re
import datetime
import time

REGION = "ap-south-1"
S3_ENDPOINT = os.getenv('S3_ENDPOINT')
DB_CONFIG = {
    "host": os.getenv('DB_HOST'),
    "database": "ledgerly-db",
    "user": os.getenv('DB_USER'),
    "password": os.getenv('DB_PASS'),
    "port": 5432
}

PROMPT = '''Extract the following information from the receipt image and return ONLY a JSON object with these fields
{
   "total_amount": "amount in the format X.XX",
   "vendor_name": "full business name",
   "receipt_date": "date in YYYY-MM-DD format"
}

Requirements:
- Convert all dates to YYYY-MM-DD format regardless of input format
- Exclude currency symbol such as ₹ from total amount always
- Extract the final paid amount including tax
- Include full business name without abbreviations where possible
- In case any of the other data is unavailable, return None as its value'''


def init_services():
    """Initialize AWS and Gemini services"""
    genai.configure(api_key=os.getenv("API_KEY"))
    return (
        boto3.client('s3', region_name=REGION),
        genai.GenerativeModel(model_name="gemini-1.5-flash")
    )


def process_image(model, image_data):
    """Process receipt image using Gemini Vision API"""
    return model.generate_content([{
        'mime_type': 'image/jpeg',
        'data': base64.b64encode(image_data).decode('utf-8')
    }, PROMPT])


def extract_receipt_data(response_text):
    """Parse structured data from API response"""
    json_str = re.search(r'{.*}', response_text, re.DOTALL).group()
    return json.loads(json_str)


def prepare_db_data(object_key, data, metadata):
    """Format data for database insertion"""
    receipt_id = object_key.split('_')[0]
    receipt_date = data.get('receipt_date', datetime.date.fromtimestamp(time.time()).isoformat())
    total_amount = data.get('total_amount', '0').replace('₹', '').strip()
    vendor_name = data.get('vendor_name', 'Unknown')
    s3_url = S3_ENDPOINT + object_key

    return (receipt_id, metadata.get('user'), metadata.get('category', 'work'),
            receipt_date, vendor_name, float(total_amount), s3_url)


def insert_receipt(conn, values):
    """Insert receipt data into PostgreSQL"""
    cursor = conn.cursor()
    insert_query = """
                    INSERT INTO receipts (receipt_id, user_id, category, receipt_date, vendor_name, total_amount, s3_url)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """
    try:
        cursor.execute(insert_query, values)
        conn.commit()
        print("Successfully created record in DB")
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        cursor.close()


def lambda_handler(event, context=None):
    """Process S3 events and store receipt data"""
    s3_client, vision_model = init_services()

    for record in event['Records']:
        bucket_name = record['s3']['bucket']['name']
        object_key = record['s3']['object']['key']

        try:
            # Get image from S3
            response = s3_client.get_object(Bucket=bucket_name, Key=object_key)
            print(f"Successfully retrieved image from S3.")
            image_data = response['Body'].read()

            # Process with Vision API
            vision_response = process_image(vision_model, image_data)
            print(f"Successfully processed image with Gemini API.")
            receipt_data = extract_receipt_data(vision_response.text)

            # Store in database
            db_values = prepare_db_data(object_key, receipt_data, response['Metadata'])
            with psycopg2.connect(**DB_CONFIG) as conn:
                insert_receipt(conn, db_values)

        except Exception as e:
            error_msg = f"Error processing receipt {object_key}: {str(e)}"
            return {"statusCode": 500, "body": json.dumps(error_msg)}

    return {"statusCode": 200, "body": json.dumps("Processing complete")}

