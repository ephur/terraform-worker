import tomlkit
from setuptools import find_packages, setup

with open("README.md", "r") as fh:
    long_description = fh.read()


def _get_project_meta():
    with open("pyproject.toml") as fh:
        contents = fh.read()

    return tomlkit.parse(contents)["tool"]["poetry"]


project_info = _get_project_meta()

setup(
    name=str(project_info["name"]),
    version=str(project_info["version"]),
    packages=find_packages(exclude=["tests*"]),
    author="Richard Maynard",
    author_email="richard.maynard@gmail.com",
    description=str(project_info["name"]),
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/ephur/terraform-worker",
    include_package_data=True,
    install_requires=[
        "boto3",
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
    python_requires=">=3.7",
)
