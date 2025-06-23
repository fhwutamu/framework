"""Agent definition that generates a testbench."""

import constants
from vertexai.preview.generative_models import GenerativeModel
import vertexai
import subprocess
import tempfile
import re
import re

def extract_module_name(verilog_text: str) -> str:
    match = re.search(r'\bmodule\s+(\w+)\s*\(', verilog_text)
    return match.group(1) if match else ""

MAX_RETRIES = 20
def extract_ports(verilog_text: str):
    ports = []
    # Match input/output/inout declarations like: input [3:0] foo;
    port_decl_pattern = re.compile(r'\b(input|output|inout)\b\s*(\[[^\]]+\])?\s*(\w+)\s*;')

    for match in port_decl_pattern.finditer(verilog_text):
        direction, width, name = match.groups()
        width = width.strip() if width else ""
        ports.append({
            "direction": direction,
            "name": name,
            "width": width,
        })

    return ports

def generate_testbench(file_name_to_content: dict[str, str]) -> str:
    # Step 1: Get the content of specification.md
    vertexai.init(project="iclad-hack25stan-3721", location="us-central1")
    model = GenerativeModel("gemini-2.0-flash-001")

    spec = file_name_to_content.get("specification.md")
    ports = extract_ports(file_name_to_content.get("mutant_0.v"))
    module = extract_module_name(file_name_to_content.get("mutant_0.v"))
    if not ports or not module:
        print("Failed to extract ports or module name from mutant_0.v")
        return constants.DUMMY_TESTBENCH

    if spec is None:
        print("specification.md not found")
        return constants.DUMMY_TESTBENCH

    print("Current module:", module)


    # step 1: test plan
    prompt_plan = f"""You are a SystemVerilog verification expert. Based on the specification below, generate a detailed **step-by-step** test plan to verify the functionality of the described module.

The test plan should include:
- A brief summary of the module’s functionality.
- A list of key input and output signals to be tested.
- A structured list of test cases (TC1, TC2, ...) with:
  - A short name and purpose for each test case
  - A brief description of how the test is performed
  - The expected behavior or pass condition

Do not write any testbench code in this step.

Module Specification:
----------------
{spec}
----------------
"""
    testplan = model.generate_content(prompt_plan).text.strip()




    # Step 2: Construct the prompt for the LLM
    prompt_template = f"""You are a SystemVerilog expert. Based on the DUT specification, its ports, and the test plan provided below, generate a valid and synthesizable SystemVerilog testbench.

Testbench requirements:
- Testbench module name must be `tb`
- Do NOT use parameters or localparams
- Instantiate the DUT with correct ports
- Declare all required signals with the correct directions and bit widths
- Apply meaningful test vectors based on the test plan
- Use `repeat` loops to apply multiple test vectors where appropriate
- Use `$error(...)` to report any incorrect behavior and stop the simulation
- Use `$display("TESTS PASSED"); $finish;` only if all checks pass

DUT Module Name:
----------------
{module}
----------------
DUT Ports:
----------------
{ports}
----------------
Test Plan:
----------------
{testplan}
----------------
Specification:
----------------
{spec}
----------------

Return only the testbench code inside a ```systemverilog``` code block.

"""

    # Step 3: Try generating and verifying the testbench up to MAX_RETRIES times
    for attempt in range(1, MAX_RETRIES + 1):
        print(f"[Attempt {attempt}] Generating testbench")

        try:
            response_text = model.generate_content(prompt_template).text
        except Exception as e:
            print(f"LLM call failed: {e}")
            return constants.DUMMY_TESTBENCH

        # Step 4: Extract the systemverilog code block
        match = re.search(r"```systemverilog\s+(.*?)```", response_text, re.DOTALL)
        if not match:
            print("No valid SystemVerilog code block found")
            continue

        testbench_code = match.group(1).strip()

        # Step 5: Write code to a temp file and check it using iverilog
        with tempfile.NamedTemporaryFile(suffix=".sv", mode="w", delete=False) as f:
            f.write(testbench_code)
            tmp_path = f.name

        with tempfile.NamedTemporaryFile(suffix=".v", mode="w", delete=False) as f:
            f.write(file_name_to_content.get("mutant_0.v", ""))
            tmp_dut_path = f.name

        # Step 6: Use iverilog to compile and check for syntax errors
        compile_result = subprocess.run(
            ["iverilog", "-g2012", tmp_path,tmp_dut_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        if compile_result.returncode == 0:
            print("Testbench passed syntax check")
            return testbench_code
        else:
            print("Syntax error detected:")
            print(compile_result.stderr)

    # Step 7: Fallback if all attempts fail
    print("Exceeded maximum attempts, returning default testbench")
    
   

    return constants.DUMMY_TESTBENCH






def run_iverilog(filenames):
    try:
        subprocess.run(["iverilog", *filenames], check=True, capture_output=True)
        return True, ""
    except subprocess.CalledProcessError as e:
        return False, e.stderr.decode()