#!/usr/bin/env bash
# ROS2 Humble 一键自动安装脚本
# 适用：Jetson 设备，JetPack 6.2（基于 Ubuntu 22.04）
# 完全自动化，无需人工交互

set -e

# 设置非交互式环境变量（全局）
export DEBIAN_FRONTEND=noninteractive
export TZ=Asia/Shanghai

# Check sudo access
check_sudo() {
  yellow "[..] Checking sudo access..."
  if ! sudo -n true 2>/dev/null; then
    yellow "[..] Sudo password may be required. Please enter your password when prompted."
    sudo -v
  fi
  green "[OK] Sudo access confirmed"
}

# 颜色输出函数
green() { printf '\033[0;32m%s\033[0m\n' "$1"; }
yellow() { printf '\033[1;33m%s\033[0m\n' "$1"; }
red() { printf '\033[0;31m%s\033[0m\n' "$1"; }
blue() { printf '\033[0;34m%s\033[0m\n' "$1"; }

# Check if running as root
check_root() {
  if [[ $EUID -eq 0 ]]; then
    red "[ERROR] Please do not run this script as root user"
    exit 1
  fi
}

# Check system architecture
check_architecture() {
  local arch=$(dpkg --print-architecture)
  if [[ "$arch" != "arm64" && "$arch" != "aarch64" ]]; then
    yellow "[WARNING] Detected architecture: $arch, this script is optimized for ARM64"
  fi
  green "[OK] System architecture: $arch"
}

# Check Ubuntu version
check_ubuntu_version() {
  if [[ ! -f /etc/os-release ]]; then
    red "[ERROR] Cannot detect system version"
    exit 1
  fi
  
  source /etc/os-release
  if [[ "$ID" != "ubuntu" ]]; then
    yellow "[WARNING] Detected non-Ubuntu system: $ID"
  fi
  
  if [[ "$VERSION_ID" != "22.04" ]]; then
    yellow "[WARNING] Detected Ubuntu version: $VERSION_ID, ROS2 Humble recommends Ubuntu 22.04"
  fi
  
  green "[OK] System version: $PRETTY_NAME"
}

# Setup locale (non-interactive)
setup_locale() {
  yellow "[..] Configuring system locale..."
  
  # Install locales if not installed
  sudo apt-get update -qq || true
  sudo apt-get install -y locales || true
  
  # Generate and set UTF-8 locale (non-interactive)
  echo "en_US.UTF-8 UTF-8" | sudo tee -a /etc/locale.gen > /dev/null 2>&1 || true
  sudo locale-gen en_US.UTF-8 > /dev/null 2>&1 || true
  sudo update-locale LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 > /dev/null 2>&1 || true
  
  export LANG=en_US.UTF-8
  export LC_ALL=en_US.UTF-8
  
  green "[OK] Locale configuration completed"
}

# Install basic dependencies
install_basic_dependencies() {
  yellow "[..] Installing basic dependencies..."
  
  if ! sudo apt-get update -qq; then
    red "[ERROR] Failed to update package list"
    exit 1
  fi
  
  if ! sudo apt-get install -y --no-install-recommends \
    curl \
    gnupg \
    lsb-release \
    ca-certificates \
    software-properties-common \
    build-essential \
    python3-pip; then
    red "[ERROR] Failed to install basic dependencies"
    exit 1
  fi
  
  green "[OK] Basic dependencies installed"
}

# Add ROS2 repository
add_ros2_repository() {
  yellow "[..] Adding ROS2 Humble repository..."
  
  # Download and install ROS key
  if [[ ! -f /usr/share/keyrings/ros-archive-keyring.gpg ]]; then
    sudo curl -sSL https://raw.githubusercontent.com/ros/rosdistro/master/ros.key \
      -o /usr/share/keyrings/ros-archive-keyring.gpg || {
      red "[ERROR] Failed to download ROS key"
      exit 1
    }
  fi
  
  # Get Ubuntu codename
  source /etc/os-release
  local ubuntu_codename=$UBUNTU_CODENAME
  if [[ -z "$ubuntu_codename" ]]; then
    ubuntu_codename="jammy"  # Default codename for Ubuntu 22.04
  fi
  
  # Add ROS2 repository
  local arch=$(dpkg --print-architecture)
  local ros2_sources="/etc/apt/sources.list.d/ros2-latest.list"
  
  if [[ ! -f "$ros2_sources" ]] || ! grep -q "ros2" "$ros2_sources" 2>/dev/null; then
    echo "deb [arch=${arch} signed-by=/usr/share/keyrings/ros-archive-keyring.gpg] http://packages.ros.org/ros2/ubuntu ${ubuntu_codename} main" | \
      sudo tee "$ros2_sources" > /dev/null
  fi
  
  green "[OK] ROS2 repository added"
}

# Install ROS2-specific dependencies (after ROS2 repository is added)
install_ros2_dependencies() {
  yellow "[..] Installing ROS2-specific dependencies..."
  
  # Update package list to include ROS2 repository
  if ! sudo apt-get update -qq; then
    yellow "[WARNING] Failed to update package list, but continuing..."
  fi
  
  # Install ROS2-specific packages (these are optional, so don't fail if they can't be installed)
  if sudo apt-get install -y --no-install-recommends \
    python3-colcon-common-extensions; then
    green "[OK] ROS2-specific dependencies installed"
  else
    yellow "[WARNING] Failed to install python3-colcon-common-extensions, but continuing..."
    yellow "[INFO] This package is optional and may not be available on all systems"
    green "[OK] Continuing with installation (some optional packages skipped)"
  fi
}

# Install ROS2 Humble Desktop
install_ros2_humble() {
  yellow "[..] Updating package list..."
  if ! sudo apt-get update -qq; then
    red "[ERROR] Failed to update package list"
    exit 1
  fi
  
  yellow "[..] Installing ROS2 Humble Desktop (this may take a while, please wait)..."
  
  # Use -y flag to auto-confirm all prompts, --no-install-recommends to reduce unnecessary packages
  if ! sudo apt-get install -y --no-install-recommends ros-humble-desktop; then
    red "[ERROR] Failed to install ROS2 Humble Desktop"
    yellow "[INFO] Trying to show detailed error messages..."
    sudo apt-get install -y --no-install-recommends ros-humble-desktop
    exit 1
  fi
  
  green "[OK] ROS2 Humble Desktop installed"
}

# Install ROS2 development tools
install_ros2_tools() {
  yellow "[..] Installing ROS2 development tools..."
  
  if ! sudo apt-get install -y \
    python3-rosdep \
    python3-argcomplete \
    python3-vcstool; then
    yellow "[WARNING] Some ROS2 development tools may have failed to install, continuing..."
  fi
  
  green "[OK] ROS2 development tools installed"
}

# Initialize and update rosdep
setup_rosdep() {
  yellow "[..] Initializing rosdep..."
  
  # Check if already initialized
  if [[ ! -f /etc/ros/rosdep/sources.list.d/20-default.list ]]; then
    # Use yes command to auto-confirm all prompts
    yes | sudo rosdep init > /dev/null 2>&1 || {
      yellow "[WARNING] rosdep may already be initialized, continuing..."
    }
  fi
  
  yellow "[..] Updating rosdep database (this may take some time)..."
  # Set timeout to avoid long waits
  timeout 300 rosdep update > /dev/null 2>&1 || {
    yellow "[WARNING] rosdep update may have failed or timed out, but this does not affect basic usage"
  }
  
  green "[OK] rosdep configuration completed"
}

# Setup environment variables
setup_environment() {
  yellow "[..] Configuring ROS2 environment variables..."
  
  local bashrc="$HOME/.bashrc"
  local setup_line="source /opt/ros/humble/setup.bash"
  
  # Check if already configured
  if ! grep -q "source /opt/ros/humble/setup.bash" "$bashrc" 2>/dev/null; then
    echo "" >> "$bashrc"
    echo "# ROS2 Humble environment setup" >> "$bashrc"
    echo "$setup_line" >> "$bashrc"
    green "[OK] Environment variables added to ~/.bashrc"
  else
    green "[OK] Environment variables already exist, skipping"
  fi
  
  # Apply immediately - source bashrc to load all environment variables
  yellow "[..] Loading environment variables..."
  source "$bashrc" 2>/dev/null || true
  source /opt/ros/humble/setup.bash 2>/dev/null || true
  
  green "[OK] Environment variables configured and loaded"
}

# Install common ROS2 packages and dependencies
install_additional_packages() {
  yellow "[..] Installing common ROS2 packages..."
  
  sudo apt-get install -y \
    ros-humble-rclpy \
    ros-humble-rclcpp \
    ros-humble-ament-cmake \
    ros-humble-ament-index-python \
    > /dev/null 2>&1 || true
  
  green "[OK] Common ROS2 packages installed"
}

# Verify installation
verify_installation() {
  yellow "[..] Verifying ROS2 Humble installation..."
  
  # Check if ROS2 is installed
  if [[ -d /opt/ros/humble ]]; then
    green "[OK] ROS2 Humble installation directory exists: /opt/ros/humble"
  else
    red "[ERROR] ROS2 Humble installation directory does not exist"
    exit 1
  fi
  
  # Check environment setup file
  if [[ -f /opt/ros/humble/setup.bash ]]; then
    green "[OK] ROS2 Humble setup.bash exists"
  else
    red "[ERROR] ROS2 Humble setup.bash does not exist"
    exit 1
  fi
  
  # Try to load environment and check command
  source /opt/ros/humble/setup.bash 2>/dev/null || true
  
  if command -v ros2 &>/dev/null; then
    local ros2_version=$(ros2 --version 2>/dev/null || echo "unknown version")
    green "[OK] ROS2 command available: $ros2_version"
  else
    yellow "[WARNING] ros2 command not available, may need to reload terminal"
  fi
  
  green "[OK] Installation verification completed"
}

# Show completion information
show_completion_info() {
  echo ""
  green "========== ROS2 Humble Installation Completed =========="
  echo ""
  blue "Installation location: /opt/ros/humble"
  blue "Configuration file: ~/.bashrc"
  echo ""
  yellow "Important notes:"
  echo "  1. Environment variables have been automatically loaded in this session."
  echo "     For new terminal sessions, environment will be loaded automatically from ~/.bashrc"
  echo ""
  echo "  2. Verify installation:"
  echo "     ros2 --help"
  echo ""
  echo "  3. Run example nodes:"
  echo "     ros2 run demo_nodes_cpp talker"
  echo "     ros2 run demo_nodes_py listener"
  echo ""
  echo "  4. Create a workspace:"
  echo "     mkdir -p ~/ros2_ws/src"
  echo "     cd ~/ros2_ws"
  echo "     colcon build"
  echo ""
  green "========================================================"
  echo ""
}

# Main function
main() {
  green "========== ROS2 Humble One-Click Auto Installation Script =========="
  green "Target System: Jetson JetPack 6.2 (Ubuntu 22.04)"
  green "ROS2 Version: Humble Hawksbill"
  echo ""
  
  # Execute installation steps
  check_root
  check_sudo
  check_architecture
  check_ubuntu_version
  setup_locale
  install_basic_dependencies
  add_ros2_repository
  install_ros2_dependencies
  install_ros2_humble
  install_ros2_tools
  setup_rosdep
  setup_environment
  install_additional_packages
  verify_installation
  show_completion_info
}

# 运行主函数
main "$@"
