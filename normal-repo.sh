find . -type f -name 'values.yaml' -exec sed -i 's|10.22.17.152:5000/oran-ric/|nexus3.o-ran-sc.org:10002/o-ran-sc/|g' {} +
