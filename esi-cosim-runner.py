#!/usr/bin/python3.10

# ===- circt-rtl-sim.py - CIRCT simulation driver -----------*- python -*-===//
#
# Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
# See https://llvm.org/LICENSE.txt for license information.
# SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
#
# ===---------------------------------------------------------------------===//
#
# Script to drive CIRCT cosimulation tests.
#
# ===---------------------------------------------------------------------===//

import argparse
import os
import re
import signal
import socket
import subprocess
import sys
import time

ThisFileDir = os.path.dirname(__file__)


class CosimTestRunner:
  """The main class responsible for running a cosim test. We use a separate
    class to allow for per-test mutable state variables."""

  def __init__(self, testFile, schema, tmpdir, addlArgs, interactive: bool,
               include_aux_files: bool, exec: bool, exec_args: str,
               server_only: bool, sim: str):
    """Parse a test file. Look for comments we recognize anywhere in the
        file. Assemble a list of sources."""

    self.server_only = server_only
    self.args = addlArgs
    self.file = testFile
    self.interactive = interactive
    self.exec = exec
    self.exec_args = exec_args
    self.runs = list()
    self.srcdir = os.path.dirname(self.file)
    self.sources = list()
    self.top = "top"
    self.tmpdir = tmpdir
    self.sim = sim

    esiInclude = "/home/parallels/circt/build/lib/Dialect/ESI"

    if "" == "" or not os.path.exists(""):
      raise Exception("The ESI cosimulation DPI library must be " +
                      "enabled to run cosim tests.")

    self.simRunScript = os.path.join("/home/parallels/circt/build/bin", "circt-rtl-sim.py")

    if schema == "":
      schema = os.path.join(esiInclude, "runtime", "cosim", "CosimDpi.capnp")
    self.schema = schema

    if not self.exec and not self.server_only:
      fileReader = open(self.file, "r")
      for line in fileReader:
        # Run this Python line.
        m = re.match(r"^(//|#)\s*PY:(.*)$", line)
        if m:
          self.runs.append(m.group(2).strip())
      fileReader.close()

    self.sources = []
    # Include the cosim DPI SystemVerilog files.
    if include_aux_files:
      self.sources += [
          os.path.join(esiInclude, "runtime", "cosim", "Cosim_DpiPkg.sv"),
          os.path.join(esiInclude, "runtime", "cosim", "Cosim_Endpoint.sv"),
          os.path.join(esiInclude, "runtime", "cosim", "Cosim_Manifest.sv"),
          os.path.join(esiInclude, "runtime", "cosim", "Cosim_MMIO.sv"),
          os.path.join(esiInclude, "ESIPrimitives.sv")
      ]
    self.sources.append("")

  def compile(self):
    """Compile with circt-rtl-sim.py"""
    start = time.time()

    # Run the simulation compilation step. Requires a simulator to be
    # installed and working.
    cmd = [self.simRunScript, "--no-objdir", "--no-run"]
    if self.sim != "":
      cmd = cmd + ["--sim", self.sim]
    cmd = cmd + self.sources + self.args
    print("[INFO] Compile command: " + " ".join(cmd))
    vrun = subprocess.run(cmd, capture_output=True, text=True)
    output = vrun.stdout + "\n----- STDERR ------\n" + vrun.stderr
    if vrun.returncode != 0:
      print("====== Compilation failure:")
      print(output)
    print(f"[INFO] Compile time: {time.time()-start}")
    return vrun.returncode

  def writeScript(self, port):
    """Write out the test script."""

    with open("script.py", "w") as script:
      # Include a bunch of config variables at the beginning of the
      # script for use by the test code.
      vars = {
          "srcdir": self.srcdir,
          "srcfile": self.file,
          # 'rpcSchemaPath' points to the CapnProto schema for RPC and is
          # the one that nearly all scripts are going to need.
          "rpcschemapath": self.schema
      }
      script.writelines(
          f"{name} = \"{value}\"\n" for (name, value) in vars.items())
      script.write("\n\n")

      # Add the test files directory and this files directory to the
      # pythonpath.
      script.write("import os\n")
      script.write("import sys\n")
      script.write(f"sys.path.append(\"{os.path.dirname(self.file)}\")\n")
      script.write(f"sys.path.append(\"{os.path.dirname(__file__)}\")\n")
      script.write("\n\n")
      script.write(f"tmpdir = '{self.tmpdir}'\n")
      script.write("simhostport = f'{os.uname()[1]}:" + str(port) + "'\n")

      # Run the lines specified in the test file.
      script.writelines(f"{x}\n" for x in self.runs)

  def run(self):
    """Run the test by creating a Python script, starting the simulation,
        running the Python script, then stopping the simulation. Use
        circt-rtl-sim.py to run the sim."""

    # These two variables are accessed in the finally block. Declare them
    # here to avoid syntax errors in that block.
    testProc = None
    simProc = None
    try:
      start = time.time()

      # Open log files
      simStdout = open("sim_stdout.log", "w")
      simStderr = open("sim_stderr.log", "w")
      if self.interactive:
        testStdout = None
        testStderr = None
      else:
        testStdout = open("test_stdout.log", "w")
        testStderr = open("test_stderr.log", "w")

      # Erase the config file if it exists. We don't want to read
      # an old config.
      portFileName = "cosim.cfg"
      if os.path.exists(portFileName):
        os.remove(portFileName)

      # Run the simulation.
      simEnv = os.environ.copy()
      if "Release" == "Debug":
        simEnv["COSIM_DEBUG_FILE"] = "cosim_debug.log"
      cmd = [self.simRunScript, "--no-objdir"]
      if self.sim != "":
        cmd = cmd + ["--sim", self.sim]
      cmd = cmd + self.sources + self.args
      print("[INFO] Sim run command: " + " ".join(cmd))
      simProc = subprocess.Popen(cmd,
                                 stdout=simStdout,
                                 stderr=simStderr,
                                 env=simEnv,
                                 preexec_fn=os.setsid)
      simStderr.close()
      simStdout.close()

      # Get the port which the simulation RPC selected.
      checkCount = 0
      while (not os.path.exists(portFileName)) and \
              simProc.poll() is None:
        time.sleep(0.1)
        checkCount += 1
        if checkCount > 200:
          raise Exception(f"Cosim never wrote cfg file: {portFileName}")
      port = -1
      while port < 0:
        portFile = open(portFileName, "r")
        for line in portFile.readlines():
          m = re.match("port: (\\d+)", line)
          if m is not None:
            port = int(m.group(1))
        portFile.close()

      # Wait for the simulation to start accepting RPC connections.
      checkCount = 0
      while not isPortOpen(port):
        checkCount += 1
        if checkCount > 200:
          raise Exception(f"Cosim RPC port ({port}) never opened")
        if simProc.poll() is not None:
          raise Exception("Simulation exited early")
        time.sleep(0.05)

      # Write the test script.
      if not self.exec:
        self.writeScript(port)

      if self.server_only:
        # wait for user input to kill the server
        input(
            f"Running in server-only mode on port {port} - Press anything to kill the server..."
        )
      else:
        # Test mode
        # Pycapnp complains if the PWD environment var doesn't match the
        # actual CWD.
        testEnv = os.environ.copy()
        testEnv["PWD"] = os.getcwd()
        testEnv["PYTHONPATH"] = testEnv[
            "PYTHONPATH"] + f":{os.path.dirname(__file__)}"
        # Run the test script.
        if self.exec:
          args = ["cosim", f"localhost:{port}", self.schema
                 ] + self.exec_args.split(" ")
          if self.file.endswith(".py"):
            cmd = [sys.executable, "-u", self.file] + args
          else:
            cmd = [self.file] + args
        else:
          cmd = [sys.executable, "-u", "script.py"]

        # strip out empty args
        cmd = [x for x in cmd if x]
        print("[INFO] Test run command: " + " ".join(cmd))
        testProc = subprocess.run(cmd,
                                  stdout=testStdout,
                                  stderr=testStderr,
                                  cwd=os.getcwd(),
                                  env=testEnv)
        if not self.interactive:
          testStdout.close()
          testStderr.close()
    finally:
      # Make sure to stop the simulation no matter what.
      if simProc:
        os.killpg(os.getpgid(simProc.pid), signal.SIGINT)
        # simProc.send_signal(signal.SIGINT)
        # Allow the simulation time to flush its outputs.
        try:
          simProc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
          simProc.kill()

      print(f"[INFO] Run time: {time.time()-start}")

      # Read the output log files and return the proper result.
      err, logs = self.readLogs()
      if testProc is not None:
        logs += f"---- Test process exit code: {testProc.returncode}\n"
        passed = testProc.returncode == 0 and not err
      else:
        passed = False
      if not passed:
        print(logs)

    return 0 if passed else 1

  def readLogs(self):
    """Read the log files from the simulation and the test script. Only add
        the stderr logs if they contain something. Also return a flag
        indicating that one of the stderr logs has content."""

    foundErr = False
    ret = "----- Simulation stdout -----\n"
    with open("sim_stdout.log") as f:
      ret += f.read()

    with open("sim_stderr.log") as f:
      stderr = f.read()
      if stderr != "":
        ret += "\n----- Simulation stderr -----\n"
        ret += stderr
        foundErr = True

      ret += "\n----- Test stdout -----\n"
    with open("test_stdout.log") as f:
      ret += f.read()

    with open("test_stderr.log") as f:
      stderr = f.read()
      if stderr != "":
        ret += "\n----- Test stderr -----\n"
        ret += stderr
        foundErr = True

    return (foundErr, ret)


def isPortOpen(port):
  sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
  result = sock.connect_ex(('127.0.0.1', port))
  sock.close()
  return True if result == 0 else False


def __main__(args):
  argparser = argparse.ArgumentParser(
      description="HW cosimulation runner for ESI")
  argparser.add_argument("-i",
                         "--interactive",
                         action="store_true",
                         help="Run the script in the foreground.")
  argparser.add_argument("--sim",
                         type=str,
                         default="",
                         help="Name of the RTL simulator (if in PATH) to " +
                         "use or path to an executable.")
  argparser.add_argument("--schema", default="", help="The schema file to use.")
  argparser.add_argument(
      "--tmpdir",
      default="",
      help="A temp dir to which files may have been generated.")
  argparser.add_argument("--no-aux-files",
                         action="store_true",
                         help="Don't include the ESI cosim auxiliary files.")
  argparser.add_argument(
      "--exec",
      action="store_true",
      help="Instead of inline python, run an executable or python "
      "script with the sim port and schema path as the first two arguments.")
  argparser.add_argument("--test-args",
                         default="",
                         help="Extra args to pass to the test.")
  argparser.add_argument(
      "--server-only",
      action="store_true",
      help="Only run the cosim server, and do not run any test files")

  argparser.add_argument("source",
                         help="The source run spec file",
                         default=None)
  argparser.add_argument("addlArgs",
                         nargs=argparse.REMAINDER,
                         help="Additional arguments to pass through to " +
                         "'circt-rtl-sim.py'")
  if len(args) <= 1:
    argparser.print_help()
    return
  args = argparser.parse_args(args[1:])

  # Create and cd into a test directory before running
  sourceName = os.path.basename(args.source)
  testDir = f"{sourceName}.d"
  if not os.path.exists(testDir):
    os.mkdir(testDir)
  os.chdir(testDir)

  runner = CosimTestRunner(args.source, args.schema, args.tmpdir, args.addlArgs,
                           args.interactive, not args.no_aux_files, args.exec,
                           args.test_args, args.server_only, args.sim)
  rc = runner.compile()
  if rc != 0:
    return rc
  return runner.run()


if __name__ == '__main__':
  sys.exit(__main__(sys.argv))
