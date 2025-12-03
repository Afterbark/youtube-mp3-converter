#!/usr/bin/env python3
"""
Pre-flight check before starting the app
Run this to verify everything is set up correctly
"""

import sys
import subprocess

def check_python_version():
    """Check Python version"""
    version = sys.version_info
    if version.major >= 3 and version.minor >= 9:
        print(f"✓ Python {version.major}.{version.minor}.{version.micro}")
        return True
    else:
        print(f"✗ Python {version.major}.{version.minor}.{version.micro} - Need 3.9+")
        return False

def check_dependencies():
    """Check if required packages are installed"""
    packages = {
        'flask': 'Flask',
        'flask_cors': 'flask-cors',
        'yt_dlp': 'yt-dlp'
    }
    
    all_installed = True
    for module, package in packages.items():
        try:
            __import__(module)
            print(f"✓ {package}")
        except ImportError:
            print(f"✗ {package} - Install with: pip install {package} --break-system-packages")
            all_installed = False
    
    return all_installed

def check_file_exists():
    """Check if app.py exists"""
    import os
    if os.path.exists('app.py'):
        print("✓ app.py found")
        return True
    else:
        print("✗ app.py not found in current directory")
        return False

def check_syntax():
    """Check Python syntax"""
    try:
        import ast
        with open('app.py', 'r') as f:
            ast.parse(f.read())
        print("✓ Python syntax valid")
        return True
    except SyntaxError as e:
        print(f"✗ Syntax error: {e}")
        return False

def check_port():
    """Check if port 5000 is available"""
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    result = sock.connect_ex(('127.0.0.1', 5000))
    sock.close()
    
    if result == 0:
        print("⚠️  Port 5000 is already in use - you may need to kill existing process")
        print("   Run: lsof -ti:5000 | xargs kill -9")
        return False
    else:
        print("✓ Port 5000 available")
        return True

def main():
    print("=" * 60)
    print("YouTube Converter - Pre-flight Check")
    print("=" * 60)
    print()
    
    checks = [
        ("Python Version", check_python_version),
        ("Dependencies", check_dependencies),
        ("App File", check_file_exists),
        ("Syntax Check", check_syntax),
        ("Port Availability", check_port),
    ]
    
    results = []
    for name, check_func in checks:
        print(f"\n{name}:")
        print("-" * 40)
        results.append(check_func())
    
    print()
    print("=" * 60)
    
    if all(results):
        print("✓ All checks passed! You can start the app:")
        print()
        print("  python app.py")
        print()
        print("Then open: http://localhost:5000")
    else:
        print("✗ Some checks failed. Please fix the issues above.")
        sys.exit(1)

if __name__ == "__main__":
    main()
