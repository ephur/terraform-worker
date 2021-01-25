from setuptools import setup, find_packages

with open("README.md", "r") as fh:
    long_description = fh.read()

setup(
    name="terraform-worker",
    version="0.7.3",
    packages=find_packages(exclude=["tests*"]),
    author="Richard Maynard",
    author_email="richard.maynard@gmail.com",
    description="An orchestration tool for Terraform",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/ephur/terraform-worker",
    include_package_data=True,
    install_requires=[
        "boto3",
        "hvac",
        "pyhcl",
        "tenacity",
        "cryptography",
        "click",
        "Jinja2",
        "PyYAML",
        "requests",
    ],
    classifiers=[
        "Development Status :: 4 - Beta",
        "Environment :: Console",
        "Intended Audience :: System Administrators",
        "Intended Audience :: Information Technology",
        "License :: OSI Approved :: Apache Software License",
        "Natural Language :: English",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3.8",
        "Topic :: System :: Systems Administration",
    ],
    entry_points={"console_scripts": ["worker=tfworker.cli:cli"]},
    setup_requires=["flake8"],
    python_requires=">=3.6",
)
