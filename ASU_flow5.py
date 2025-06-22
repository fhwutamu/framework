import yaml
import subprocess
from vertexai.preview.generative_models import GenerativeModel
import vertexai
import time
import re

MAX_ITER = 10

PDK_MAP = {
    "SkyWater 130HD": {
        "lib":  "/OpenROAD-flow-scripts/flow/platforms/sky130hd/lib/sky130_fd_sc_hd__tt_025C_1v80.lib",
        "lef":  "/OpenROAD-flow-scripts/flow/platforms/sky130hd/lef/sky130_fd_sc_hd_merged.lef",
        "tlef": "/OpenROAD-flow-scripts/flow/platforms/sky130hd/lef/sky130_fd_sc_hd.tlef"
    },
    "SkyWater 130HS": {
        "lib":  "/OpenROAD-flow-scripts/flow/platforms/sky130hs/lib/sky130_fd_sc_hs__tt_025C_1v80.lib",
        "lef":  "/OpenROAD-flow-scripts/flow/platforms/sky130hs/lef/sky130_fd_sc_hs_merged.lef",
        "tlef": "/OpenROAD-flow-scripts/flow/platforms/sky130hs/lef/sky130_fd_sc_hs.tlef"
    },
    "Nangate45": {
        "lib":  "/OpenROAD-flow-scripts/flow/platforms/nangate45/lib/NangateOpenCellLibrary_typical.lib",
        "lef":  "/OpenROAD-flow-scripts/flow/platforms/nangate45/lef/NangateOpenCellLibrary.macro.lef",
        "tlef": "/OpenROAD-flow-scripts/flow/platforms/nangate45/lef/NangateOpenCellLibrary.tech.lef"
    },
    "ASAP7": {
        "lib":  "/OpenROAD-flow-scripts/flow/platforms/asap7/lib/CCS/asap7sc7p5t_SIMPLE_RVT_FF_ccs_211120.lib",
        "lef":  "/OpenROAD-flow-scripts/flow/platforms/asap7/lef/asap7sc7p5t_DFFHV2X.lef",
        "tlef": None
    }
}

def write_file(filename, content):
    with open(filename, 'w') as f:
        f.write(content)


def run_yosys_synth(top_module, design_file):
    spec = yaml.safe_load(open("spec.yaml"))
    tech_node = spec[top_module].get("tech_node")
    pdk = PDK_MAP.get(tech_node)

    if not pdk or not pdk.get("lib"):
        print(f"❌ Cannot synthesize: missing liberty file for tech_node {tech_node}")
        return

    lib_path = pdk["lib"]
    yosys_script = f"""
read_verilog {design_file}
hierarchy -check -top {top_module}
read_liberty -lib {lib_path}
synth -top {top_module}
dfflibmap -liberty {lib_path}
abc -liberty {lib_path}
write_verilog {top_module}_netlist.v
"""
    write_file("synth.ys", yosys_script)
    print("\n⚙️ Running Yosys synthesis...")
    subprocess.run(["yosys", "-s", "synth.ys"], check=True)



def fix_output_reg_syntax(design_file):
    with open(design_file, 'r') as f:
        code = f.read()

    pattern = r'output\s+reg\s+(\w+)\s*;'
    matches = re.findall(pattern, code)

    if not matches:
        return

    def replacer(match):
        name = match.group(1)
        return f'output {name};\nreg {name};'

    code = re.sub(r'output\s+reg\s+(\w+)\s*;', replacer, code)

    with open(design_file, 'w') as f:
        f.write(code)

def run_iverilog(filenames):
    try:
        subprocess.run(["iverilog", *filenames], check=True, capture_output=True)
        return True, ""
    except subprocess.CalledProcessError as e:
        return False, e.stderr.decode()

def strip_markdown_code_blocks(verilog_code):
    matches = re.findall(r'```(?:verilog|systemverilog)?\n(.*?)```', verilog_code, flags=re.DOTALL | re.IGNORECASE)
    return matches[-1].strip("\n") if matches else verilog_code.strip()

def refine_with_gemini(prompt, model, top_module):
    design_file = f"iclad_{top_module}.v"
    for _ in range(MAX_ITER):
        response = model.generate_content(prompt).text
        verilog = strip_markdown_code_blocks(response)
        write_file(design_file, verilog)
        success, err = run_iverilog([design_file])
        if success:
            print("\n✅ Verilog syntax is correct.")
            return verilog
        prompt = f"The following Verilog has syntax errors:\n{err}\nPlease fix them:\n{verilog}"
    return None

def refine_testbench_with_gemini(tb_prompt, model, design_file, tb_file):
    for _ in range(MAX_ITER):
        response = model.generate_content(tb_prompt).text
        tb_code = strip_markdown_code_blocks(response)
        write_file(tb_file, tb_code)

        success, err = run_iverilog([design_file, tb_file])
        if success:
            print("\n✅ Testbench syntax is correct.")
            return True
        else:
            print("\n❌ Testbench syntax error, retrying...")
            tb_prompt = f"The following testbench has syntax errors:\n{err}\nPlease fix it:\n{tb_code}"
    return False

def generate_sdc(top_module, clock_period):
    sdc = f"""current_design {top_module}

set clk_name  clk
set clk_port_name clk
set clk_period {round(clock_period - 0.1, 3)}
set clk_io_pct 0.2

set clk_port [get_ports $clk_port_name]

create_clock -name $clk_name -period $clk_period $clk_port

set non_clock_inputs [lsearch -inline -all -not -exact [all_inputs] $clk_port]

set_input_delay  [expr $clk_period * $clk_io_pct] -clock $clk_name $non_clock_inputs 
set_output_delay [expr $clk_period * $clk_io_pct] -clock $clk_name [all_outputs]
"""
    write_file("constraint.sdc", sdc)

def prompt_continue_openroad():
    user_input = input("\n❗Functionality test failed after all attempts. Proceed to OpenROAD synthesis anyway? (Y/N): ").strip().lower()
    return user_input == 'y'

def run_openroad_flow(top_module, design_file):
    spec = yaml.safe_load(open("spec.yaml"))
    tech_node = spec[top_module].get("tech_node")
    pdk = PDK_MAP.get(tech_node)
    if not pdk:
        print(f"Unsupported tech_node: {tech_node}")
        return

    tlef_cmd = f"read_lef {pdk['tlef']}\n" if pdk['tlef'] else ""
    tcl_script = f"""
{tlef_cmd}read_lef {pdk['lef']}
read_liberty {pdk['lib']}

read_verilog {design_file}

link_design {top_module}

read_sdc constraint.sdc

initialize_floorplan -utilization 0.4 -aspect_ratio 1.0 -core_space 2 -site unithd

place_io_terminals
global_placement
detailed_placement
write_def {top_module}_placed.def

tapcell
# run_filler

run_routing

write_def {top_module}_routed.def

report_wns > {top_module}_wns.rpt
report_tns > {top_module}_tns.rpt
report_power > {top_module}_power.rpt

write_db {top_module}.odb
"""
    write_file("run_openroad.tcl", tcl_script)
    print("\n🚀 Launching OpenROAD...")
    subprocess.run(
        f"bash -c 'source /OpenROAD-flow-scripts/env.sh && openroad run_openroad.tcl'",
        shell=True,
        executable="/bin/bash"
    )

def verify_functionality(top_module, design_file, testbench_file):
    functionality_passed = False
    for _ in range(MAX_ITER):
        success, err = run_iverilog([design_file, testbench_file])
        if success:
            functionality_passed = True
            print("\n✅ Functionality test passed.")
            user_input = input("Proceed to OpenROAD synthesis and place-and-route? (Y/N): ").strip().lower()
            if user_input == 'y':
                run_yosys_synth(top_module, design_file)
                run_openroad_flow(top_module, f"{top_module}_netlist.v")
            break
        else:
            print("\n❌ Functionality test failed. Retrying...\n")
            print(err)

    if not functionality_passed:
        if prompt_continue_openroad():
            run_yosys_synth(top_module, design_file)
            run_openroad_flow(top_module, f"{top_module}_netlist.v")

def make_testbench_prompt(top_module, content):
    ports = content.get("ports", [])
    parameters = content.get("parameters", {})
    sample_input = content.get("sample_input")
    sample_output = content.get("sample_output")
    sample_usage = content.get("sample_usage")

    param_str = "\n".join([f"- {k}: {v}" for k, v in parameters.items()])
    port_str = "\n".join([
        f"- {p['direction']} {p['name']} ({p['type']}{'[' + str(p['width']) + ']' if 'width' in p else ''})"
        for p in ports
    ])

    if sample_input and sample_output:
        case_desc = f"Stimulus:\n{sample_input}\nExpected:\n{sample_output}"
    elif sample_usage:
        case_desc = f"Sample Usage:\n{sample_usage}"
    else:
        case_desc = "No specific test case provided."

    prompt = f"""Please write a Verilog testbench for the following module.

Module: {top_module}

Ports:
{port_str}

Parameters:
{param_str or 'None'}

{case_desc}

The testbench should:
- Instantiate the module correctly using actual port names
- Apply stimulus to inputs
- Monitor outputs and print PASS/FAIL
- Use `$display` to show actual vs expected output

Important:
- Use correct parameter and port names
- Always write in standard Verilog (2005) format
- Do not invent extra ports or parameters
- Do not include extra comments or Markdown code block syntax
"""
    return prompt

def prompt_from_yaml(spec):
    name, content = list(spec.items())[0]
    description = content.get("description", "")
    ports = content.get("ports", [])
    module_sig = content.get("module_signature", "")
    parameters = content.get("parameters", {})

    port_lines = []
    for p in ports:
        port_lines.append(f"- {p['direction']} {p['type']} [{p['width'] if 'width' in p else ''}] {p['name']}: {p.get('description','')}\n")

    param_lines = [f"- {k}: {v}" for k, v in parameters.items()] if parameters else []

    prompt = """Please act as a professional digital hardware designer.

1.Module Name: {}

2.Description: 
{}
3.Ports:
{}
4.Parameters:
{}

5.Pseudocode Implementation:
Plan the pseudocode implementation of this design function

6.Module Signature:
{}

7.Verilog Implementation:
Convert the pseudocode into complete, synthesizable Verilog code for the Module Signature frame.

Important:
- Provide only the final Verilog code under section 7.
- Always write in standard Verilog (2005) format
- Do not include additional explanations or comments outside the code block.

""".format(name, description, ''.join(port_lines), '\n'.join(param_lines), module_sig)

    write_file("current_design.txt", prompt)
    return name, content.get("clock_period", 1.0), content

def main():
    vertexai.init(project="iclad-hack25stan-3721", location="us-central1")
    model = GenerativeModel("gemini-2.0-flash-001")

    spec = yaml.safe_load(open("spec.yaml"))
    top_module, clock_period, content = prompt_from_yaml(spec)
    design_prompt = open("current_design.txt").read()
    generate_sdc(top_module, float(str(clock_period).lower().replace("ns", "").strip()))

    print("\n🔧 Generating Verilog with Gemini...")
    verilog_code = refine_with_gemini(design_prompt, model, top_module)
    design_file = f"iclad_{top_module}.v"

    print("\n🧪 Generating testbench with Gemini...")
    tb_prompt = make_testbench_prompt(top_module, content)
    tb_file = f"{top_module}_tb.v"
    success = refine_testbench_with_gemini(tb_prompt, model, design_file, tb_file)
    clean_tb_code = strip_markdown_code_blocks(tb_file)

    if not success:
        print("⚠️ Testbench could not be fixed after several attempts.")
        write_file(tb_file, clean_tb_code)

    print("\n🔧 To the 2005 Verilog...")
    fix_output_reg_syntax(design_file)
    print("\n🧪 Running functionality test...")
    verify_functionality(top_module, design_file, tb_file)

if __name__ == "__main__":
    main()
