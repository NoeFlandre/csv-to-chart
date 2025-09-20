import os
from flask import Flask, request, render_template, jsonify
import pandas as pd
from openai import OpenAI
import re
import shutil

# Set matplotlib backend to non-interactive before importing pyplot
import matplotlib
matplotlib.use('Agg')  # Use non-interactive backend

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'


@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload_file():
    print("Upload route called")  # Debug log
    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400
    if file and file.filename.endswith('.csv'):
        filename = file.filename
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)
        print(f"File saved to: {filepath}")  # Debug log
        return jsonify({'filepath': filepath})
    else:
        return jsonify({'error': 'Invalid file type. Please upload a CSV file.'}), 400

@app.route('/use-sample', methods=['POST'])
def use_sample():
    """Create a copy of the sample CSV file for use"""
    try:
        # Copy the sample.csv to the uploads folder with a unique name
        sample_path = 'sample.csv'
        if not os.path.exists(sample_path):
            return jsonify({'error': 'Sample file not found'}), 404
            
        # Create a unique filename
        import time
        filename = f"sample_{int(time.time())}.csv"
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        
        # Copy the sample file
        shutil.copy2(sample_path, filepath)
        
        return jsonify({'filepath': filepath})
    except Exception as e:
        return jsonify({'error': f'Error using sample file: {str(e)}'}), 500

@app.route('/chart', methods=['POST'])
def generate_chart():
    data = request.get_json()
    print(f"Received data: {data}")  # Debug log
    
    if not data:
        return jsonify({'error': 'Invalid JSON data'}), 400
        
    filepath = data.get('filepath')
    question = data.get('question')
    api_key = data.get('api_key')

    print(f"Filepath: {filepath}")  # Debug log
    print(f"Question: {question}")  # Debug log
    print(f"API Key: {api_key}")    # Debug log

    if not filepath or not question:
        return jsonify({'error': 'Missing filepath or question'}), 400
        
    if not api_key:
        return jsonify({'error': 'Missing API key. Please provide your OpenRouter API key.'}), 400

    # Validate that the file exists and is a CSV
    if not os.path.exists(filepath) or not filepath.endswith('.csv'):
        return jsonify({'error': 'Invalid file path or file type'}), 400

    try:
        df = pd.read_csv(filepath)
        if df.empty:
            return jsonify({'error': 'CSV file is empty'}), 400
            
        df_head = df.head().to_string()

        prompt = f"""
        Here is the head of a CSV file:
        {df_head}

        The user has asked the following question: "{question}"

        Based on this question and the CSV data, generate Python code using the Matplotlib library to create a chart that answers the question.
        The full CSV file is located at: {filepath}
        The code should save the chart to a file named 'chart.png' in the 'static' directory.
        The code should be a single block of Python code.
        Do not include any explanations or comments outside of the code block.
        Your response should only contain the python code in a markdown block.
        Make sure to:
        1. Import all necessary libraries (matplotlib.pyplot as plt, pandas as pd, etc.) in the code
        2. Set matplotlib to use a non-interactive backend with 'matplotlib.use('Agg')' before importing pyplot
        3. Load the full CSV data from the provided filepath
        4. Handle potential errors gracefully
        5. Use appropriate chart types for the data and question
        6. Add proper labels and titles to make the chart informative
        7. Save the chart as 'static/chart.png'
        """

        # Configure the OpenRouter API client with the provided key
        client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=api_key,
        )

        completion = client.chat.completions.create(
            model="deepseek/deepseek-chat-v3.1:free",
            messages=[
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
        )
        code_response = completion.choices[0].message.content

        # Extract the Python code from the response
        code_match = re.search(r'```python\s*(.*?)\s*```', code_response, re.DOTALL)
        if not code_match:
            return jsonify({'error': 'Failed to extract Python code from AI response'}), 500
            
        code = code_match.group(1)

        # Execute the generated code
        try:
            exec(code)
        except Exception as e:
            print(f"Error executing generated code: {e}")
            return jsonify({'error': f'Error executing generated code: {str(e)}'}), 500

        return jsonify({'chart_url': '/static/chart.png'})

    except pd.errors.EmptyDataError:
        return jsonify({'error': 'CSV file is empty or invalid'}), 400
    except pd.errors.ParserError:
        return jsonify({'error': 'CSV file format is invalid'}), 400
    except Exception as e:
        # Check if it's an API authentication error
        error_msg = str(e)
        print(f"Exception occurred: {error_msg}")  # Debug log
        if "401" in error_msg or "authentication" in error_msg.lower() or "unauthorized" in error_msg.lower():
            return jsonify({'error': 'API authentication failed. Please check your OpenRouter API key.'}), 401
        return jsonify({'error': f'Error processing request: {str(e)}'}), 500

if __name__ == '__main__':
    if not os.path.exists('uploads'):
        os.makedirs('uploads')
    if not os.path.exists('static'):
        os.makedirs('static')
    app.run(debug=True, port=5001)