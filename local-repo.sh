find . -type f -name 'values.yaml' -exec sed -i 's|nexus3.o-ran-sc.org:10002/o-ran-sc/|10.22.17.156:5000/oran-ric/|g' {} +
