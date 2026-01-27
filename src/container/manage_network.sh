#!/bin/bash
# Network isolation management script for Docker container
# This script allows manual control of network isolation (iptables rules)

set -e

SCRIPT_NAME="manage_network.sh"
USAGE="Usage: $SCRIPT_NAME {enable|disable|status|test}

Commands:
  enable   - Enable network isolation (block external internet access)
  disable  - Disable network isolation (restore full internet access)
  status   - Show current iptables OUTPUT rules
  test     - Test external connectivity (curl Aliyun DNS 223.5.5.5 and baidu.com)
"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Check if running as root or with NET_ADMIN capability
if [ "$EUID" -ne 0 ] && ! capsh --print | grep -q "NET_ADMIN"; then
    echo -e "${RED}Error: This script requires root privileges or NET_ADMIN capability${NC}" >&2
    echo "Container must be started with --cap-add=NET_ADMIN" >&2
    exit 1
fi

# Check if iptables is available
if ! command -v iptables &> /dev/null; then
    echo -e "${RED}Error: iptables command not found${NC}" >&2
    echo "Please install iptables in the container" >&2
    exit 1
fi

enable_isolation() {
    echo -e "${YELLOW}Enabling network isolation...${NC}"
    
    # Flush existing OUTPUT rules
    iptables -F OUTPUT 2>/dev/null || true
    
    # Allow localhost
    iptables -A OUTPUT -d 127.0.0.0/8 -j ACCEPT 2>/dev/null || true
    
    # Allow Docker bridge network (172.17.0.0/16) - critical for host-container communication
    iptables -A OUTPUT -d 172.17.0.0/16 -j ACCEPT 2>/dev/null || true
    
    # Allow other private networks
    iptables -A OUTPUT -d 172.16.0.0/12 -j ACCEPT 2>/dev/null || true
    iptables -A OUTPUT -d 10.0.0.0/8 -j ACCEPT 2>/dev/null || true
    iptables -A OUTPUT -d 192.168.0.0/16 -j ACCEPT 2>/dev/null || true
    
    # Drop all other outbound traffic (external internet)
    iptables -A OUTPUT -j DROP 2>/dev/null || true
    
    echo -e "${GREEN}✓ Network isolation enabled${NC}"
    echo "  - External internet access: ${RED}BLOCKED${NC}"
    echo "  - Docker bridge network (172.17.0.0/16): ${GREEN}ALLOWED${NC}"
    echo "  - Private networks: ${GREEN}ALLOWED${NC}"
}

disable_isolation() {
    echo -e "${YELLOW}Disabling network isolation...${NC}"
    
    # Flush OUTPUT rules to restore internet access
    iptables -F OUTPUT 2>/dev/null || true
    
    echo -e "${GREEN}✓ Network isolation disabled${NC}"
    echo "  - External internet access: ${GREEN}RESTORED${NC}"
}

show_status() {
    echo -e "${YELLOW}Current iptables OUTPUT rules:${NC}"
    echo ""
    
    if iptables -L OUTPUT -n -v 2>/dev/null | grep -q "Chain OUTPUT"; then
        iptables -L OUTPUT -n -v 2>/dev/null
        echo ""
        
        # Check if DROP rule exists
        if iptables -L OUTPUT -n -v 2>/dev/null | grep -q "DROP"; then
            echo -e "Status: ${RED}Network isolation is ENABLED${NC}"
        else
            echo -e "Status: ${GREEN}Network isolation is DISABLED${NC}"
        fi
    else
        echo -e "${YELLOW}No OUTPUT rules found (isolation disabled)${NC}"
    fi
}

test_connectivity() {
    echo -e "${YELLOW}Testing external connectivity...${NC}"
    echo ""
    
    # Test curl to Aliyun DNS server (223.5.5.5) via HTTP
    echo -n "Testing curl to Aliyun DNS (223.5.5.5): "
    if timeout 3 curl -s --connect-timeout 2 http://223.5.5.5 &>/dev/null; then
        echo -e "${GREEN}SUCCESS${NC}"
    else
        echo -e "${RED}FAILED${NC} (expected if isolation is enabled)"
    fi
    
    # Test curl to baidu.com
    echo -n "Testing curl to https://www.baidu.com: "
    if timeout 3 curl -s --connect-timeout 2 https://www.baidu.com &>/dev/null; then
        echo -e "${GREEN}SUCCESS${NC}"
    else
        echo -e "${RED}FAILED${NC} (expected if isolation is enabled)"
    fi
    
    # Test Docker bridge network (should always work)
    echo -n "Testing Docker bridge network (172.17.0.1): "
    if timeout 2 curl -s --connect-timeout 1 http://172.17.0.1:8080 &>/dev/null || [ $? -eq 7 ]; then
        # Exit code 7 is "Failed to connect" which is OK - means network is reachable
        echo -e "${GREEN}REACHABLE${NC}"
    else
        echo -e "${RED}UNREACHABLE${NC}"
    fi
}

# Main command handling
case "${1:-}" in
    enable)
        enable_isolation
        ;;
    disable)
        disable_isolation
        ;;
    status)
        show_status
        ;;
    test)
        test_connectivity
        ;;
    "")
        echo "$USAGE"
        exit 1
        ;;
    *)
        echo -e "${RED}Error: Unknown command '${1}'${NC}" >&2
        echo ""
        echo "$USAGE"
        exit 1
        ;;
esac

