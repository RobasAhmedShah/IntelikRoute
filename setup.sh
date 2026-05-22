#!/bin/bash
set -e

echo "=== IntelikRoute Automated Setup ==="

# 1. Detect Operating System and install system dependency (miniupnpc)
OS="$(uname -s)"
echo "Detecting operating system: $OS"

if [ "$OS" = "Darwin" ]; then
    if command -v brew >/dev/null 2>&1; then
        echo "Installing miniupnpc via Homebrew..."
        brew install miniupnpc
    else
        echo "Warning: Homebrew not found. Please install Homebrew first or install 'miniupnpc' manually."
    fi
elif [ "$OS" = "Linux" ]; then
    if command -v apt-get >/dev/null 2>&1; then
        echo "Installing miniupnpc via apt-get..."
        sudo apt-get update && sudo apt-get install -y miniupnpc
    elif command -v yum >/dev/null 2>&1; then
        echo "Installing miniupnpc via yum..."
        sudo yum install -y miniupnpc
    else
        echo "Warning: Supported package manager not found. Please install 'miniupnpc' manually."
    fi
else
    echo "Warning: Unsupported OS ($OS). Please install 'miniupnpc' manually."
fi

# 2. Install IntelikRoute python CLI package
echo ""
echo "Installing IntelikRoute Python package..."
python3 -m pip install -e . --user --break-system-packages

# 3. Handle PATH configuration for the installed binary
echo ""
echo "Checking PATH configuration..."
PYTHON_USER_BASE=$(python3 -m site --user-base)
if [ -n "$PYTHON_USER_BASE" ]; then
    USER_BIN="$PYTHON_USER_BASE/bin"
else
    USER_BIN="$HOME/Library/Python/3.14/bin"
fi

if [[ ":$PATH:" != *":$USER_BIN:"* ]]; then
    echo "The directory $USER_BIN is not in your PATH."
    if [ -f "$HOME/.zshrc" ]; then
        echo "Adding $USER_BIN to PATH in ~/.zshrc..."
        echo "export PATH=\"$USER_BIN:\$PATH\"" >> "$HOME/.zshrc"
        echo "PATH updated in ~/.zshrc! Please run: source ~/.zshrc"
    elif [ -f "$HOME/.bash_profile" ]; then
        echo "Adding $USER_BIN to PATH in ~/.bash_profile..."
        echo "export PATH=\"$USER_BIN:\$PATH\"" >> "$HOME/.bash_profile"
        echo "PATH updated in ~/.bash_profile! Please run: source ~/.bash_profile"
    else
        echo "Could not auto-update shell configuration. Please add $USER_BIN to your system PATH."
    fi
else
    echo "PATH is already configured correctly."
fi

echo ""
echo "=== IntelikRoute Setup Completed Successfully ==="
echo "If this is a new terminal window, please reload your shell config or open a new window."
echo "You can then run: intelikroute --help"
